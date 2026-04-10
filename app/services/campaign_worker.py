"""
Campaign Worker - Background process for executing campaigns
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import random
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models import (
    Campaign, CampaignNumber, User,
    CampaignStatus, CallStatus, VoiceProvider, VoxCallerIDVerificationStatus, CampaignMode
)
from app.services.ai_call_runtime_service import (
    AICallRuntimeService,
    cleanup_ai_runtime_artifacts,
    prune_stale_ai_runtime_artifacts,
    update_campaign_number_ai_observability,
)
from app.services.ai_campaign_readiness_service import get_ai_campaign_readiness, get_ai_schema_health
from app.config import get_settings
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
from app.services.worker_health_service import touch_worker_heartbeat

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
settings = get_settings()
WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")
MIN_CONCURRENT_CALLS = 1
MAX_CONCURRENT_CALLS = 20
TWILIO_MIN_START_INTERVAL_SECONDS = 1.0
SIGNALWIRE_MIN_START_INTERVAL_SECONDS = 3.0
SIGNALWIRE_MAX_START_INTERVAL_SECONDS = 5.0


class _ThreadSafeStartRateLimiter:
    """Simple shared limiter for outbound call starts."""

    def __init__(self, interval_seconds: float | Callable[[], float]):
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait_turn(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                time.sleep(self._next_allowed_at - now)
                now = time.monotonic()
            self._next_allowed_at = now + self._current_interval_seconds()

    def _current_interval_seconds(self) -> float:
        if callable(self._interval_seconds):
            return float(self._interval_seconds())
        return float(self._interval_seconds)


class CampaignWorker:
    """Worker for processing campaign calls"""

    def __init__(self, db: Session):
        self.db = db
        self.running = True

    def _pause_campaign(
        self,
        db: Session,
        campaign: Campaign,
        reason: str,
        *,
        campaign_number: CampaignNumber | None = None,
    ) -> None:
        message = str(reason or "Campaign paused by worker").strip()[:500]
        campaign.status = CampaignStatus.PAUSED
        db.commit()

        if campaign.campaign_mode != CampaignMode.AI_AGENT:
            return

        target_number = campaign_number
        if target_number is None:
            target_number = db.query(CampaignNumber).filter(
                CampaignNumber.campaign_id == campaign.id,
            ).order_by(CampaignNumber.id).first()

        if target_number is None:
            return

        db.query(CampaignNumber).filter(
            CampaignNumber.id == target_number.id
        ).update(
            {
                CampaignNumber.error_message: message,
            },
            synchronize_session=False,
        )
        db.commit()
        update_campaign_number_ai_observability(
            target_number.id,
            ai_runtime_error=message,
        )

    def process_pending_campaigns(self):
        """Find and process all running campaigns"""
        removed_artifacts = prune_stale_ai_runtime_artifacts()
        if removed_artifacts:
            logger.info("Pruned %s stale AI runtime artifact(s)", removed_artifacts)

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
                self._pause_campaign(
                    self.db,
                    campaign,
                    f"Worker crashed while processing campaign: {str(e)[:420]}",
                )

    def process_campaign(self, campaign: Campaign):
        """Process a single campaign"""
        user = campaign.user
        logger.info(f"Processing campaign {campaign.id}: {campaign.name}")

        # Check user has active rental.
        if not has_active_rental(self.db, user.id):
            reason = "Rental expired or inactive"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return

        # Check user has transfer number configured
        if not user.transfer_number:
            reason = "Transfer number is not configured for this user"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return

        # Enforce tenant ownership for resources.
        if campaign.caller_id.user_id != user.id:
            reason = "Caller ID ownership mismatch"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return
        if campaign.audio_id is not None and campaign.audio is None:
            reason = "Selected audio resource was not found"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return
        if campaign.audio and campaign.audio.user_id != user.id:
            reason = "Audio ownership mismatch"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return
        if campaign.ai_agent and campaign.ai_agent.user_id != user.id:
            reason = "AI agent ownership mismatch"
            logger.warning(f"Campaign {campaign.id}: {reason}, pausing")
            self._pause_campaign(self.db, campaign, reason)
            return

        # Validate provider credentials before dispatching calls.
        try:
            self._build_voice_service(campaign, user)
        except Exception as e:
            reason = f"Failed to initialize AI runtime: {str(e)[:420]}"
            logger.error(f"Campaign {campaign.id}: {reason}")
            self._pause_campaign(self.db, campaign, reason)
            return

        max_concurrent_calls = self._normalize_concurrency(campaign.max_concurrent_calls)
        batch_size = max(5, max_concurrent_calls)
        provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)
        start_rate_limiter = self._build_start_rate_limiter(provider)

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

                    if start_rate_limiter:
                        start_rate_limiter.wait_turn()

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
                reason = "Rental expired while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return

            if not user.transfer_number:
                reason = "Transfer number missing while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return

            if campaign.caller_id.user_id != user.id:
                reason = "Caller ID ownership mismatch while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return
            if campaign.audio_id is not None and campaign.audio is None:
                reason = "Selected audio resource was not found while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return
            if campaign.audio and campaign.audio.user_id != user.id:
                reason = "Audio ownership mismatch while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return
            if campaign.ai_agent and campaign.ai_agent.user_id != user.id:
                reason = "AI agent ownership mismatch while processing a campaign number"
                logger.warning(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
                return

            try:
                voice_service = self._build_voice_service(campaign, user, db=db)
            except Exception as e:
                reason = f"Failed to initialize AI runtime while processing a campaign number: {str(e)[:390]}"
                logger.error(f"Campaign {campaign.id}: {reason}")
                self._pause_campaign(db, campaign, reason, campaign_number=number)
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
            # Make the call using campaign-configured TwiML flow.
            call_result = voice_service.make_call(
                to_number=phone_number,
                from_number=caller_id.phone_number,
                audio_url=audio.r2_url if audio else None,
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
            handoff_stop_event = threading.Event()
            handoff_listener_started = False

            def _listen_handoff_events() -> None:
                from app.services.ai_realtime_event_bus import AIRealtimeEvent, AIRealtimeEventBus

                bus = AIRealtimeEventBus()

                def stop_when() -> bool:
                    return handoff_stop_event.is_set()

                def on_event(event: AIRealtimeEvent) -> None:
                    if event.event != "tool.transfer_call":
                        return
                    reason = str((event.payload or {}).get("reason") or "Tool transfer requested").strip()
                    call_sid = str(call_result.get("call_sid") or "")
                    twiml = f"""<Response>
    <Dial callerId="{caller_id.phone_number}" timeout="30">
        <Number>{user.transfer_number}</Number>
    </Dial>
</Response>"""
                    try:
                        voice_service.update_call_twiml(call_sid, twiml)
                        update_campaign_number_ai_observability(
                            number.id,
                            ai_handoff_reason=reason[:500],
                        )
                    except Exception as exc:
                        update_campaign_number_ai_observability(
                            number.id,
                            ai_runtime_error=f"[tooling] transfer_call failed: {str(exc)[:420]}",
                        )

                bus.consume(
                    number.id,
                    stop_when=stop_when,
                    on_event=on_event,
                )

            if (
                campaign.campaign_mode == CampaignMode.AI_AGENT
                and bool(getattr(voice_service, "handoff_via_event_bus", False))
            ):
                handoff_listener_started = True
                handoff_thread = threading.Thread(
                    target=_listen_handoff_events,
                    daemon=True,
                    name=f"ai-handoff-{number.id}",
                )
                handoff_thread.start()

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
                max_wait=(
                    int(settings.AI_POLL_MAX_WAIT_SECONDS)
                    if campaign.campaign_mode == CampaignMode.AI_AGENT
                    else 70
                ),
                status_callback=persist_status_update,
                metadata={"campaign_number_id": number.id},
            )
            if handoff_listener_started:
                handoff_stop_event.set()

            final_status = self._map_status(final_result['status'])
            final_duration = int(final_result.get('duration') or 0)
            final_answered_by = final_result.get('answered_by')
            final_provider_error_code = final_result.get('error_code')
            final_provider_error_message = str(final_result.get('error_message') or "").strip()

            # Calculate cost based on duration and country price
            if final_duration > 0:
                minutes = (final_duration + 59) // 60  # Round up
                cost = minutes * country.price_per_minute
            else:
                cost = 0.0

            ai_policy_error = self._ai_runtime_policy_error(campaign, final_duration, cost)
            error_message = None
            if ai_policy_error:
                final_status = CallStatus.FAILED
                error_message = ai_policy_error
                update_campaign_number_ai_observability(
                    number.id,
                    ai_runtime_error=ai_policy_error,
                )
            elif final_status != CallStatus.COMPLETED and (
                final_provider_error_message or final_provider_error_code
            ):
                provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)
                error_message = (
                    f"{provider.upper()} final status={final_result.get('status')} "
                    f"code={final_provider_error_code} message={final_provider_error_message}"
                )[:500]
                if campaign.campaign_mode == CampaignMode.AI_AGENT:
                    update_campaign_number_ai_observability(
                        number.id,
                        ai_runtime_error=error_message,
                    )

            db.query(CampaignNumber).filter(
                CampaignNumber.id == number.id
            ).update(
                {
                    CampaignNumber.status: final_status,
                    CampaignNumber.duration_seconds: final_duration,
                    CampaignNumber.answered_by: final_answered_by,
                    CampaignNumber.processed_at: datetime.utcnow(),
                    CampaignNumber.cost: cost,
                    CampaignNumber.error_message: error_message,
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

            if ai_policy_error:
                db.query(Campaign).filter(Campaign.id == campaign.id).update(
                    {Campaign.status: CampaignStatus.PAUSED},
                    synchronize_session=False,
                )
                db.commit()
                logger.warning(
                    "Campaign %s paused due to AI runtime policy violation on number %s: %s",
                    campaign.id,
                    number.id,
                    ai_policy_error,
                )

            if campaign.campaign_mode == CampaignMode.AI_AGENT:
                cleanup_ai_runtime_artifacts(number.id)

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
            if campaign.campaign_mode == CampaignMode.AI_AGENT:
                update_campaign_number_ai_observability(
                    number.id,
                    ai_runtime_error=str(e),
                )
                cleanup_ai_runtime_artifacts(number.id)

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

    def _build_start_rate_limiter(self, provider: str) -> Optional[_ThreadSafeStartRateLimiter]:
        """Return provider-specific outbound call pacing rules."""
        if provider == VoiceProvider.TWILIO.value:
            return _ThreadSafeStartRateLimiter(TWILIO_MIN_START_INTERVAL_SECONDS)

        if provider == VoiceProvider.SIGNALWIRE.value:
            return _ThreadSafeStartRateLimiter(
                lambda: random.uniform(
                    SIGNALWIRE_MIN_START_INTERVAL_SECONDS,
                    SIGNALWIRE_MAX_START_INTERVAL_SECONDS,
                )
            )

        return None

    def _ai_runtime_policy_error(
        self,
        campaign: Campaign,
        final_duration: int,
        cost: float,
    ) -> str | None:
        if campaign.campaign_mode != CampaignMode.AI_AGENT:
            return None

        max_duration = int(settings.AI_MAX_CALL_DURATION_SECONDS)
        max_cost = float(settings.AI_MAX_CALL_COST_USD)
        if max_duration > 0 and final_duration > max_duration:
            return (
                f"AI call exceeded max duration policy: {final_duration}s > {max_duration}s"
            )
        if max_cost > 0 and cost > max_cost:
            return f"AI call exceeded max cost policy: ${cost:.3f} > ${max_cost:.3f}"
        return None

    def _build_voice_service(self, campaign: Campaign, user: User, db: Optional[Session] = None):
        session = db or self.db
        provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)

        if campaign.campaign_mode == CampaignMode.AI_AGENT:
            if provider != VoiceProvider.TWILIO.value:
                raise ValueError("AI agent campaigns currently require Twilio")
            if not campaign.ai_agent or not campaign.ai_agent.is_active:
                raise ValueError("AI agent is not configured or inactive")
            readiness = get_ai_campaign_readiness(
                session,
                user_id=user.id,
                voice_provider=provider,
                ai_agent=campaign.ai_agent,
            )
            if readiness.error:
                raise ValueError(readiness.error)
            return AICallRuntimeService(session, user.id, provider=provider)

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
    schema_health = get_ai_schema_health()
    if not schema_health["ready"]:
        logger.error("AI schema is incomplete after init_db: %s", ", ".join(schema_health["missing_items"]))

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
        touch_worker_heartbeat()
    except Exception as e:
        logger.warning(f"Could not update worker heartbeat: {e}")


if __name__ == "__main__":
    run_worker()
