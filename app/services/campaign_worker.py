"""
Campaign Worker - Background process for executing campaigns
"""
import logging
import signal
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Campaign, CampaignNumber, User,
    CampaignStatus, CallStatus
)
from app.services.twilio_service import TwilioService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CampaignWorker:
    """Worker for processing campaign calls"""

    def __init__(self, db: Session):
        self.db = db
        self.running = True

    def process_pending_campaigns(self):
        """Find and process all running campaigns"""
        campaigns = self.db.query(Campaign).filter(
            Campaign.status == CampaignStatus.RUNNING
        ).all()

        logger.info(f"Found {len(campaigns)} running campaigns")

        for campaign in campaigns:
            if not self.running:
                break

            try:
                self.process_campaign(campaign)
            except Exception as e:
                logger.error(f"Error processing campaign {campaign.id}: {e}")
                campaign.status = CampaignStatus.PAUSED
                self.db.commit()

    def process_campaign(self, campaign: Campaign):
        """Process a single campaign"""
        user = campaign.user
        logger.info(f"Processing campaign {campaign.id}: {campaign.name}")

        # Check user has credits
        if user.credits <= 0:
            logger.warning(f"Campaign {campaign.id}: User has no credits, pausing")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Check user has transfer number configured
        if not user.transfer_number:
            logger.warning(f"Campaign {campaign.id}: User transfer number not configured, pausing")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Initialize Twilio service (uses global credentials)
        try:
            twilio_service = TwilioService()
        except Exception as e:
            logger.error(f"Campaign {campaign.id}: Failed to init Twilio: {e}")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Get pending numbers (process in batches of 5)
        pending_numbers = self.db.query(CampaignNumber).filter(
            CampaignNumber.campaign_id == campaign.id,
            CampaignNumber.status == CallStatus.PENDING
        ).limit(5).all()

        if not pending_numbers:
            # Campaign complete
            campaign.status = CampaignStatus.COMPLETED
            campaign.completed_at = datetime.utcnow()

            self.db.commit()
            logger.info(f"Campaign {campaign.id} completed")
            return

        for number in pending_numbers:
            if not self.running:
                break

            # Refresh campaign status in case it was paused
            self.db.refresh(campaign)
            if campaign.status != CampaignStatus.RUNNING:
                logger.info(f"Campaign {campaign.id} no longer running, stopping")
                break

            # Check credits before each call
            if user.credits <= 0:
                logger.warning(f"Campaign {campaign.id}: Credits exhausted")
                campaign.status = CampaignStatus.PAUSED
                self.db.commit()
                break

            self.process_number(campaign, number, twilio_service)

            # Delay between calls
            if self.running:
                time.sleep(5)

        self.db.commit()

    def process_number(
        self,
        campaign: Campaign,
        number: CampaignNumber,
        twilio_service: TwilioService
    ):
        """Process a single number in a campaign"""
        user = campaign.user
        caller_id = campaign.caller_id
        audio = campaign.audio
        country = campaign.country

        logger.info(f"Calling {number.phone_number} for campaign {campaign.id}")

        # Mark as queued
        number.status = CallStatus.QUEUED
        self.db.flush()

        try:
            # Make the call with inline TwiML (plays audio then transfers)
            call_result = twilio_service.make_call(
                to_number=number.phone_number,
                from_number=caller_id.phone_number,
                audio_url=audio.r2_url,
                transfer_number=user.transfer_number
            )

            number.call_sid = call_result['call_sid']
            number.status = CallStatus.RINGING
            self.db.flush()

            # Poll for completion
            final_result = twilio_service.poll_call_status(call_result['call_sid'])

            # Update number record
            number.status = self._map_status(final_result['status'])
            number.duration_seconds = final_result['duration']
            number.answered_by = final_result['answered_by']
            number.processed_at = datetime.utcnow()

            # Calculate cost based on duration and country price
            if final_result['duration'] > 0:
                minutes = (final_result['duration'] + 59) // 60  # Round up
                cost = minutes * country.price_per_minute
            else:
                cost = 0.0

            number.cost = cost

            # Deduct from user credits
            user.credits -= cost

            # Update campaign stats
            campaign.processed_numbers += 1
            campaign.total_cost += cost

            if number.status == CallStatus.COMPLETED:
                campaign.successful_calls += 1
            else:
                campaign.failed_calls += 1

            logger.info(
                f"Call to {number.phone_number}: "
                f"{number.status.value}, duration={final_result['duration']}s, cost=${cost:.4f}"
            )

        except Exception as e:
            import traceback
            logger.error(f"Error calling {number.phone_number}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            number.status = CallStatus.FAILED
            number.error_message = str(e)[:500]  # Limit error message length
            number.processed_at = datetime.utcnow()
            campaign.processed_numbers += 1
            campaign.failed_calls += 1

    def _map_status(self, twilio_status: str) -> CallStatus:
        """Map Twilio status to CallStatus enum"""
        mapping = {
            'completed': CallStatus.COMPLETED,
            'in-progress': CallStatus.IN_PROGRESS,
            'no-answer': CallStatus.NO_ANSWER,
            'busy': CallStatus.BUSY,
            'failed': CallStatus.FAILED,
            'canceled': CallStatus.CANCELLED,
            'timeout': CallStatus.FAILED,
        }
        return mapping.get(twilio_status, CallStatus.FAILED)

    def stop(self):
        """Signal worker to stop"""
        logger.info("Worker stop requested")
        self.running = False


def run_worker(check_interval: int = 10):
    """
    Main worker loop

    Args:
        check_interval: Seconds between campaign checks
    """
    logger.info("Starting campaign worker...")

    # Handle graceful shutdown
    worker = None

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        if worker:
            worker.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    while True:
        db = SessionLocal()
        try:
            worker = CampaignWorker(db)
            worker.process_pending_campaigns()

            if not worker.running:
                break

        except Exception as e:
            logger.error(f"Worker error: {e}")
        finally:
            db.close()

        # Wait before next check
        logger.debug(f"Sleeping for {check_interval}s...")
        time.sleep(check_interval)

    logger.info("Campaign worker stopped")


if __name__ == "__main__":
    run_worker()
