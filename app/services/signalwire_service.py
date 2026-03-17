"""
SignalWire Voice service (Compatibility API via direct HTTP requests)
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class SignalWireService:
    """Service for making SignalWire calls through the Compatibility API."""

    def __init__(self, project_id: str, api_token: str, space_url: str):
        if not project_id or not api_token or not space_url:
            raise ValueError("SignalWire credentials not configured")

        self.project_id = project_id.strip()
        self.api_token = api_token.strip()
        self.space_url = self._normalize_space_url(space_url)
        self.base_url = (
            f"https://{self.space_url}/api/laml/2010-04-01/Accounts/{self.project_id}"
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
        audio_url: Optional[str],
        transfer_number: str,
        campaign_id: Optional[int] = None,
        press_1_to_talk_with_agent: bool = False,
        timeout: int = 60,
        metadata: Optional[dict] = None,
    ) -> dict:
        del metadata
        logger.info(f"Initiating SignalWire call to {to_number} from {from_number}")

        payload: dict[str, str | int] = {
            "To": to_number,
            "From": from_number,
            "Timeout": int(timeout),
        }
        if press_1_to_talk_with_agent:
            if campaign_id is None:
                raise ValueError("campaign_id is required when press_1_to_talk_with_agent is enabled")
            base_url = settings.BASE_URL.rstrip("/")
            payload["Url"] = f"{base_url}/api/twiml/{campaign_id}"
        else:
            if audio_url:
                twiml = f"""<Response>
            <Play>{audio_url}</Play>
            <Dial callerId="{from_number}" timeout="30">
                <Number>{transfer_number}</Number>
            </Dial>
        </Response>"""
            else:
                twiml = f"""<Response>
            <Dial callerId="{from_number}" timeout="30">
                <Number>{transfer_number}</Number>
            </Dial>
        </Response>"""
            payload["Twiml"] = twiml

        machine_detection_payload: dict[str, str | int] = {}
        if not press_1_to_talk_with_agent:
            machine_detection_payload = {
                "MachineDetection": "Enable",
                "MachineDetectionTimeout": 5,
                "MachineDetectionSpeechThreshold": 2400,
                "MachineDetectionSpeechEndThreshold": 1200,
                "MachineDetectionSilenceTimeout": 5000,
            }

        try:
            with httpx.Client(timeout=30.0, auth=(self.project_id, self.api_token)) as client:
                response = client.post(
                    f"{self.base_url}/Calls.json",
                    data={**payload, **machine_detection_payload},
                    headers={"Accept": "application/json"},
                )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"SignalWire create call failed: {exc}") from exc

        call_sid = data.get("sid")
        if not call_sid:
            raise RuntimeError(f"SignalWire create call returned no call SID: {data}")
        status = data.get("status") or "queued"

        logger.info(f"SignalWire call initiated: SID={call_sid}, status={status}")
        return {"call_sid": call_sid, "status": status}

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
                with httpx.Client(timeout=30.0, auth=(self.project_id, self.api_token)) as client:
                    response = client.get(
                        f"{self.base_url}/Calls/{call_sid}.json",
                        headers={"Accept": "application/json"},
                    )
                response.raise_for_status()
                data = response.json()
                current_status = data.get("status")
                duration = int(data.get("duration") or 0)
                answered_by = data.get("answered_by") or data.get("AnsweredBy")

                if current_status != last_status:
                    if status_callback:
                        try:
                            status_callback(
                                current_status,
                                duration,
                                answered_by,
                            )
                        except Exception as callback_error:
                            logger.warning(
                                f"Status callback error for {call_sid}: {callback_error}"
                            )
                    last_status = current_status

                if current_status in final_statuses:
                    return {
                        "status": current_status,
                        "duration": duration,
                        "answered_by": answered_by,
                    }

                time.sleep(poll_interval)
                elapsed += poll_interval
            except Exception as exc:
                logger.warning(f"SignalWire polling error for {call_sid}: {exc}")
                time.sleep(poll_interval)
                elapsed += poll_interval

        logger.warning(f"SignalWire polling timeout for call {call_sid}")
        return {"status": "timeout", "duration": 0, "answered_by": None}
