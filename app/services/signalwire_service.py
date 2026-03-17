"""
SignalWire Voice service (Compatibility API via Twilio-compatible client)
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from app.config import get_settings

try:
    from signalwire.rest import Client
except ImportError:  # pragma: no cover - handled at runtime if dependency missing
    Client = None

logger = logging.getLogger(__name__)
settings = get_settings()


class SignalWireService:
    """Service for making SignalWire calls through the Compatibility API."""

    def __init__(self, project_id: str, api_token: str, space_url: str):
        if not project_id or not api_token or not space_url:
            raise ValueError("SignalWire credentials not configured")
        if Client is None:
            raise ValueError("SignalWire SDK not installed. Please install the 'signalwire' package.")

        self.project_id = project_id.strip()
        self.api_token = api_token.strip()
        self.space_url = self._normalize_space_url(space_url)
        self.client = Client(
            self.project_id,
            self.api_token,
            signalwire_space_url=self.space_url,
        )

    def _normalize_space_url(self, space_url: str) -> str:
        normalized = (space_url or "").strip()
        if normalized.startswith("https://"):
            normalized = normalized[len("https://"):]
        elif normalized.startswith("http://"):
            normalized = normalized[len("http://"):]
        return normalized.rstrip("/")

    def make_call(
        self,
        to_number: str,
        from_number: str,
        audio_url: str,
        transfer_number: str,
        campaign_id: Optional[int] = None,
        press_1_to_talk_with_agent: bool = False,
        timeout: int = 60,
        metadata: Optional[dict] = None,
    ) -> dict:
        del metadata
        logger.info(f"Initiating SignalWire call to {to_number} from {from_number}")

        call_kwargs = {}
        if press_1_to_talk_with_agent:
            if campaign_id is None:
                raise ValueError("campaign_id is required when press_1_to_talk_with_agent is enabled")
            base_url = settings.BASE_URL.rstrip("/")
            call_kwargs["url"] = f"{base_url}/api/twiml/{campaign_id}"
        else:
            twiml = f"""<Response>
            <Play>{audio_url}</Play>
            <Dial callerId="{from_number}" timeout="30">
                <Number>{transfer_number}</Number>
            </Dial>
        </Response>"""
            call_kwargs["twiml"] = twiml

        machine_detection_kwargs = {}
        if not press_1_to_talk_with_agent:
            machine_detection_kwargs = {
                "machine_detection": "Enable",
                "machine_detection_timeout": 5,
                "machine_detection_speech_threshold": 2400,
                "machine_detection_speech_end_threshold": 1200,
                "machine_detection_silence_timeout": 5000,
            }

        try:
            call = self.client.calls.create(
                to=to_number,
                from_=from_number,
                timeout=timeout,
                **call_kwargs,
                **machine_detection_kwargs,
            )
        except Exception as exc:
            raise RuntimeError(f"SignalWire create call failed: {exc}") from exc

        logger.info(f"SignalWire call initiated: SID={call.sid}, status={call.status}")
        return {"call_sid": call.sid, "status": call.status}

    def poll_call_status(
        self,
        call_sid: str,
        max_wait: int = 70,
        poll_interval: int = 2,
        status_callback: Optional[Callable[[str, int, Optional[str]], None]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        del metadata
        elapsed = 0
        final_statuses = ["completed", "failed", "busy", "no-answer", "canceled"]
        last_status = None

        while elapsed < max_wait:
            try:
                call = self.client.calls(call_sid).fetch()
                current_status = call.status

                if current_status != last_status:
                    if status_callback:
                        try:
                            status_callback(
                                current_status,
                                int(call.duration) if call.duration else 0,
                                getattr(call, "answered_by", None),
                            )
                        except Exception as callback_error:
                            logger.warning(
                                f"Status callback error for {call_sid}: {callback_error}"
                            )
                    last_status = current_status

                if current_status in final_statuses:
                    return {
                        "status": current_status,
                        "duration": int(call.duration) if call.duration else 0,
                        "answered_by": getattr(call, "answered_by", None),
                    }

                time.sleep(poll_interval)
                elapsed += poll_interval
            except Exception as exc:
                logger.warning(f"SignalWire polling error for {call_sid}: {exc}")
                time.sleep(poll_interval)
                elapsed += poll_interval

        logger.warning(f"SignalWire polling timeout for call {call_sid}")
        return {"status": "timeout", "duration": 0, "answered_by": None}
