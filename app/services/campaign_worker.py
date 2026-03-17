"""
Campaign Worker - Background process for executing campaigns
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models import (
    Campaign, CampaignNumber, User,
    CampaignStatus, CallStatus, VoiceProvider, VoxCallerIDVerificationStatus
)
from app.services.telnyx_service import TelnyxService
from app.services.twilio_service import TwilioService
from app.services.signalwire_service import SignalWireService
from app.services.vonage_service import VonageService
from app.services.voximplant_service import VoximplantService
from app.services.rental_service import has_active_rental
from app.services.user_signalwire_service import get_user_signalwire_credentials
from app.services.user_telnyx_service import get_user_telnyx_credentials
from app.services.user_twilio_service import get_user_twilio_credentials
from app.services.user_vonage_service import get_user_vonage_credentials
from app.services.user_voximplant_service import get_user_voximplant_credentials

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")
MIN_CONCURRENT_CALLS = 1
MAX_CONCURRENT_CALLS = 20
TWILIO_MIN_START_INTERVAL_SECONDS = 1.0


class _ThreadSafeStartRateLimiter:
    """Simple shared limiter for outbound call starts."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait_turn(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                time.sleep(self._next_allowed_at - now)
                now = time.monotonic()
            self._next_allowed_at = now + self.min_interval_seconds


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

        # Validate provider credentials before dispatching calls.
        try:
            self._build_voice_service(campaign, user)
        except Exception as e:
            logger.error(f"Campaign {campaign.id}: Failed to init voice provider: {e}")
            campaign.status = CampaignStatus.PAUSED
            self.db.commit()
            return

        max_concurrent_calls = self._normalize_concurrency(campaign.max_concurrent_calls)
        batch_size = max(5, max_concurrent_calls)
        provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)
        start_rate_limiter = (
            _ThreadSafeStartRateLimiter(TWILIO_MIN_START_INTERVAL_SECONDS)
            if provider == VoiceProvider.TWILIO.value
            else None
        )

        # Keep batches bounded so multiple running campaigns still take turns.
        pending_numbers = self.db.query(CampaignNumber).filter(
            CampaignNumber.campaign_id == campaign.id,
            CampaignNumber.status == CallStatus.PENDING
        ).limit(batch_size).all()

        if not pending_numbers:
            # Campaign complete
            campaign.status = CampaignStatus.COMPLETED
            campaign.completed_at = datetime.utcnow()

            self.db.commit()
            logger.info(f"Campaign {campaign.id} completed")
            return

        pending_number_ids = [n.id for n in pending_numbers]
        futures = {}

        logger.info(
            f"Campaign {campaign.id}: dispatching {len(pending_number_ids)} numbers "
            f"with concurrency={max_concurrent_calls}"
        )

        with ThreadPoolExecutor(max_workers=max_concurrent_calls) as executor:
            while self.running and (pending_number_ids or futures):
                while self.running and pending_number_ids and len(futures) < max_concurrent_calls:
                    self.db.refresh(campaign)
                    if campaign.status != CampaignStatus.RUNNING:
                        logger.info(f"Campaign {campaign.id} no longer running, stopping")
                        pending_number_ids.clear()
                        break

                    if not has_active_rental(self.db, user.id):
                        logger.warning(f"Campaign {campaign.id}: Rental expired mid-run")
                        campaign.status = CampaignStatus.PAUSED
                        self.db.commit()
                        pending_number_ids.clear()
                        break

                    number_id = pending_number_ids.pop(0)
                    future = executor.submit(
                        self.process_number_by_id,
                        campaign.id,
                        number_id,
                        start_rate_limiter,
                    )
                    futures[future] = number_id

                if not futures:
                    continue

                done, _ = wait(
                    list(futures.keys()),
                    timeout=1.0,
                    return_when=FIRST_COMPLETED
                )

                for future in done:
                    number_id = futures.pop(future)
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(
                            f"Campaign {campaign.id}: unexpected worker error "
                            f"for number_id={number_id}: {e}"
                        )

    def process_number(
        self,
        campaign: Campaign,
        number: CampaignNumber,
        voice_service
    ):
        """Process a single number in the current DB session."""
        self._process_number_with_session(
            self.db,
            campaign,
            number,
            voice_service,
            start_rate_limiter=None,
        )

    def process_number_by_id(
        self,
        campaign_id: int,
        number_id: int,
        start_rate_limiter: Optional[_ThreadSafeStartRateLimiter] = None
    ):
        """Process one number using a dedicated session (thread-safe)."""
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            number = db.query(CampaignNumber).filter(
                CampaignNumber.id == number_id,
                CampaignNumber.campaign_id == campaign_id,
            ).first()

            if not campaign or not number:
                return
            if campaign.status != CampaignStatus.RUNNING or number.status != CallStatus.PENDING:
                return

            user = campaign.user

            if not has_active_rental(db, user.id):
                logger.warning(f"Campaign {campaign.id}: Rental expired while processing number {number.id}")
                campaign.status = CampaignStatus.PAUSED
                db.commit()
                return

            if not user.transfer_number:
                logger.warning(f"Campaign {campaign.id}: transfer number missing while processing number {number.id}")
                campaign.status = CampaignStatus.PAUSED
                db.commit()
                return

            if campaign.caller_id.user_id != user.id or campaign.audio.user_id != user.id:
                logger.warning(f"Campaign {campaign.id}: resource ownership mismatch while processing number {number.id}")
                campaign.status = CampaignStatus.PAUSED
                db.commit()
                return

            try:
                voice_service = self._build_voice_service(campaign, user, db=db)
            except Exception as e:
                logger.error(f"Campaign {campaign.id}: Failed to init voice provider: {e}")
                campaign.status = CampaignStatus.PAUSED
                db.commit()
                return

            self._process_number_with_session(
                db,
                campaign,
                number,
                voice_service,
                start_rate_limiter=start_rate_limiter,
            )
        finally:
            db.close()

    def _process_number_with_session(
        self,
        db: Session,
        campaign: Campaign,
        number: CampaignNumber,
        voice_service,
        start_rate_limiter: Optional[_ThreadSafeStartRateLimiter] = None,
    ):
        """Process a single number using the provided DB session."""
        user = campaign.user
        caller_id = campaign.caller_id
        audio = campaign.audio
        country = campaign.country
        phone_number = number.phone_number

        logger.info(f"Calling {phone_number} for campaign {campaign.id}")

        claimed_rows = db.query(CampaignNumber).filter(
            CampaignNumber.id == number.id,
            CampaignNumber.status == CallStatus.PENDING
        ).update(
            {CampaignNumber.status: CallStatus.QUEUED},
            synchronize_session=False
        )
        db.commit()
        if claimed_rows == 0:
            return

        try:
            if start_rate_limiter:
                start_rate_limiter.wait_turn()

            # Make the call using campaign-configured TwiML flow.
            call_result = voice_service.make_call(
                to_number=phone_number,
                from_number=caller_id.phone_number,
                audio_url=audio.r2_url,
                transfer_number=user.transfer_number,
                campaign_id=campaign.id,
                press_1_to_talk_with_agent=campaign.press_1_to_talk_with_agent,
                metadata={"campaign_number_id": number.id},
            )

            initial_status = self._map_status(
                call_result.get('status'),
                default=CallStatus.RINGING
            )
            db.query(CampaignNumber).filter(
                CampaignNumber.id == number.id
            ).update(
                {
                    CampaignNumber.call_sid: call_result['call_sid'],
                    CampaignNumber.status: initial_status,
                },
                synchronize_session=False
            )
            db.commit()

            live_state = {
                "status": initial_status,
                "duration": 0,
                "answered_by": None,
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
                status_callback=persist_status_update,
                metadata={"campaign_number_id": number.id},
            )

            final_status = self._map_status(final_result['status'])
            final_duration = int(final_result.get('duration') or 0)
            final_answered_by = final_result.get('answered_by')

            # Calculate cost based on duration and country price
            if final_duration > 0:
                minutes = (final_duration + 59) // 60  # Round up
                cost = minutes * country.price_per_minute
            else:
                cost = 0.0

            db.query(CampaignNumber).filter(
                CampaignNumber.id == number.id
            ).update(
                {
                    CampaignNumber.status: final_status,
                    CampaignNumber.duration_seconds: final_duration,
                    CampaignNumber.answered_by: final_answered_by,
                    CampaignNumber.processed_at: datetime.utcnow(),
                    CampaignNumber.cost: cost,
                },
                synchronize_session=False
            )

            campaign_updates = {
                Campaign.processed_numbers: Campaign.processed_numbers + 1,
                Campaign.total_cost: Campaign.total_cost + cost,
            }
            if final_status == CallStatus.COMPLETED:
                campaign_updates[Campaign.successful_calls] = Campaign.successful_calls + 1
            else:
                campaign_updates[Campaign.failed_calls] = Campaign.failed_calls + 1

            db.query(Campaign).filter(
                Campaign.id == campaign.id
            ).update(campaign_updates, synchronize_session=False)
            db.commit()

            logger.info(
                f"Call to {phone_number}: "
                f"{final_status.value}, duration={final_duration}s, cost=${cost:.4f}"
            )

        except Exception as e:
            import traceback
            logger.error(f"Error calling {phone_number}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            db.query(CampaignNumber).filter(
                CampaignNumber.id == number.id
            ).update(
                {
                    CampaignNumber.status: CallStatus.FAILED,
                    CampaignNumber.error_message: str(e)[:500],  # Limit error message length
                    CampaignNumber.processed_at: datetime.utcnow(),
                },
                synchronize_session=False
            )
            db.query(Campaign).filter(
                Campaign.id == campaign.id
            ).update(
                {
                    Campaign.processed_numbers: Campaign.processed_numbers + 1,
                    Campaign.failed_calls: Campaign.failed_calls + 1,
                },
                synchronize_session=False
            )
            db.commit()

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

    def _normalize_concurrency(self, value: Optional[int]) -> int:
        """Clamp campaign concurrency to a safe range."""
        raw = value or MIN_CONCURRENT_CALLS
        return max(MIN_CONCURRENT_CALLS, min(MAX_CONCURRENT_CALLS, int(raw)))

    def _build_voice_service(self, campaign: Campaign, user: User, db: Optional[Session] = None):
        session = db or self.db
        provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)

        if provider == VoiceProvider.TWILIO.value:
            account_sid, auth_token = get_user_twilio_credentials(session, user.id)
            return TwilioService(account_sid=account_sid, auth_token=auth_token)

        if provider == VoiceProvider.SIGNALWIRE.value:
            project_id, api_token, space_url = get_user_signalwire_credentials(session, user.id)
            return SignalWireService(project_id=project_id, api_token=api_token, space_url=space_url)

        if provider == VoiceProvider.TELNYX.value:
            api_key, account_sid = get_user_telnyx_credentials(session, user.id)
            return TelnyxService(api_key=api_key, account_sid=account_sid)

        if provider == VoiceProvider.VONAGE.value:
            application_id, private_key = get_user_vonage_credentials(session, user.id)
            return VonageService(application_id=application_id, private_key=private_key)

        if provider == VoiceProvider.VOXIMPLANT.value:
            if campaign.caller_id.vox_verification_status != VoxCallerIDVerificationStatus.VERIFIED:
                raise ValueError("Caller ID is not verified in Voximplant")
            credentials = get_user_voximplant_credentials(session, user.id)
            if not credentials:
                raise ValueError("Voximplant credentials not configured")
            return VoximplantService(session, credentials)

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
