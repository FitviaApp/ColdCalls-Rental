"""
Campaign Worker - Background process for executing campaigns
"""
import logging
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models import (
    Campaign, CampaignNumber, User,
    CampaignStatus, CallStatus, VoiceProvider
)
from app.services.telnyx_service import TelnyxService
from app.services.twilio_service import TwilioService
from app.services.vonage_service import VonageService
from app.services.rental_service import has_active_rental
from app.services.user_telnyx_service import get_user_telnyx_credentials
from app.services.user_twilio_service import get_user_twilio_credentials
from app.services.user_vonage_service import get_user_vonage_credentials

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")


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

        # Check user has active rental.
        if not has_active_rental(self.db, user.id):
            logger.warning(f"Campaign {campaign.id}: Rental expired or inactive, pausing")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Check user has transfer number configured
        if not user.transfer_number:
            logger.warning(f"Campaign {campaign.id}: User transfer number not configured, pausing")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Enforce tenant ownership for resources.
        if campaign.caller_id.user_id != user.id or campaign.audio.user_id != user.id:
            logger.warning(f"Campaign {campaign.id}: Resource ownership mismatch, pausing")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        # Initialize voice provider service with campaign owner's credentials.
        try:
            voice_service = self._build_voice_service(campaign, user)
        except Exception as e:
            logger.error(f"Campaign {campaign.id}: Failed to init voice provider: {e}")
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

            # Re-check active rental before each call.
            if not has_active_rental(self.db, user.id):
                logger.warning(f"Campaign {campaign.id}: Rental expired mid-run")
                campaign.status = CampaignStatus.PAUSED
                self.db.commit()
                break

            self.process_number(campaign, number, voice_service)

            # Delay between calls
            if self.running:
                time.sleep(5)

        self.db.commit()

    def process_number(
        self,
        campaign: Campaign,
        number: CampaignNumber,
        voice_service
    ):
        """Process a single number in a campaign"""
        user = campaign.user
        caller_id = campaign.caller_id
        audio = campaign.audio
        country = campaign.country

        logger.info(f"Calling {number.phone_number} for campaign {campaign.id}")

        # Mark as queued
        number.status = CallStatus.QUEUED
        self.db.commit()

        try:
            # Make the call using campaign-configured TwiML flow.
            call_result = voice_service.make_call(
                to_number=number.phone_number,
                from_number=caller_id.phone_number,
                audio_url=audio.r2_url,
                transfer_number=user.transfer_number,
                campaign_id=campaign.id,
                press_1_to_talk_with_agent=campaign.press_1_to_talk_with_agent
            )

            number.call_sid = call_result['call_sid']
            number.status = self._map_status(
                call_result.get('status'),
                default=CallStatus.RINGING
            )
            self.db.commit()

            live_state = {
                "status": number.status,
                "duration": number.duration_seconds or 0,
                "answered_by": number.answered_by,
            }

            def persist_status_update(provider_status: str, duration: int, answered_by: Optional[str]):
                mapped_status = self._map_status(provider_status, default=live_state["status"])
                updates = {}

                if mapped_status != live_state["status"]:
                    updates["status"] = mapped_status
                    live_state["status"] = mapped_status

                if duration > live_state["duration"]:
                    updates["duration_seconds"] = duration
                    live_state["duration"] = duration

                if answered_by and answered_by != live_state["answered_by"]:
                    updates["answered_by"] = answered_by
                    live_state["answered_by"] = answered_by

                if not updates:
                    return

                callback_db = SessionLocal()
                try:
                    callback_db.query(CampaignNumber).filter(
                        CampaignNumber.id == number.id
                    ).update(updates, synchronize_session=False)
                    callback_db.commit()
                finally:
                    callback_db.close()

            # Poll for completion
            final_result = voice_service.poll_call_status(
                call_result['call_sid'],
                status_callback=persist_status_update
            )

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
            self.db.commit()

        except Exception as e:
            import traceback
            logger.error(f"Error calling {number.phone_number}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            number.status = CallStatus.FAILED
            number.error_message = str(e)[:500]  # Limit error message length
            number.processed_at = datetime.utcnow()
            campaign.processed_numbers += 1
            campaign.failed_calls += 1
            self.db.commit()

    def _map_status(self, provider_status: str, default: CallStatus = CallStatus.FAILED) -> CallStatus:
        """Map provider status to CallStatus enum"""
        normalized_status = str(provider_status or "").strip().lower()
        mapping = {
            'pending': CallStatus.PENDING,
            'queued': CallStatus.QUEUED,
            'initiated': CallStatus.QUEUED,
            'started': CallStatus.QUEUED,
            'ringing': CallStatus.RINGING,
            'calling': CallStatus.RINGING,
            'completed': CallStatus.COMPLETED,
            'in-progress': CallStatus.IN_PROGRESS,
            'in_progress': CallStatus.IN_PROGRESS,
            'no-answer': CallStatus.NO_ANSWER,
            'no_answer': CallStatus.NO_ANSWER,
            'unanswered': CallStatus.NO_ANSWER,
            'busy': CallStatus.BUSY,
            'failed': CallStatus.FAILED,
            'canceled': CallStatus.CANCELLED,
            'cancelled': CallStatus.CANCELLED,
            'rejected': CallStatus.FAILED,
            'timeout': CallStatus.FAILED,
        }
        return mapping.get(normalized_status, default)

    def _build_voice_service(self, campaign: Campaign, user: User):
        provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)

        if provider == VoiceProvider.TWILIO.value:
            account_sid, auth_token = get_user_twilio_credentials(self.db, user.id)
            return TwilioService(account_sid=account_sid, auth_token=auth_token)

        if provider == VoiceProvider.TELNYX.value:
            api_key, account_sid = get_user_telnyx_credentials(self.db, user.id)
            return TelnyxService(api_key=api_key, account_sid=account_sid)

        if provider == VoiceProvider.VONAGE.value:
            application_id, private_key = get_user_vonage_credentials(self.db, user.id)
            return VonageService(application_id=application_id, private_key=private_key)

        raise ValueError(f"Unsupported voice provider: {provider}")

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
    # Ensure tables and lightweight schema patches are applied even if app has not started.
    init_db()

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
            _touch_heartbeat()

            if not worker.running:
                break

        except Exception as e:
            logger.error(f"Worker error: {e}")
            _touch_heartbeat()
        finally:
            db.close()

        # Wait before next check
        logger.debug(f"Sleeping for {check_interval}s...")
        time.sleep(check_interval)

    logger.info("Campaign worker stopped")


def _touch_heartbeat():
    """Update worker heartbeat timestamp for health checks."""
    try:
        WORKER_HEARTBEAT_FILE.touch()
    except Exception as e:
        logger.warning(f"Could not update worker heartbeat: {e}")


if __name__ == "__main__":
    run_worker()
