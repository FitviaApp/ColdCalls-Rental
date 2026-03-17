"""
Vonage Voice service
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable

import httpx
from jose import jwt

logger = logging.getLogger(__name__)


class VonageService:
    """Service for making Vonage Voice API calls."""

    def __init__(self, application_id: str, private_key: str):
        if not application_id or not private_key:
            raise ValueError("Vonage credentials not configured")

        self.application_id = application_id.strip()
        self.private_key = private_key.strip()
        self.base_url = "https://api.nexmo.com/v1/calls"

    def _build_jwt(self) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "application_id": self.application_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "jti": str(uuid.uuid4()),
            "acl": {
                "paths": {
                    "/v1/calls/**": {},
                    "/v1/users/**": {},
                    "/v1/conversations/**": {},
                    "/v1/sessions/**": {},
                    "/v1/devices/**": {},
                    "/v1/image/**": {},
                    "/v1/media/**": {},
                    "/v1/applications/**": {},
                    "/beta/**": {},
                }
            },
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

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
        del campaign_id, timeout, metadata
        if press_1_to_talk_with_agent:
            raise ValueError("Press 1 flow is currently supported only for Twilio, SignalWire, and Voximplant campaigns")

        ncco = [
            {
                "action": "stream",
                "streamUrl": [audio_url],
            },
            {
                "action": "connect",
                "from": from_number,
                "endpoint": [{"type": "phone", "number": transfer_number}],
                "timeout": 30,
            },
        ]

        payload = {
            "to": [{"type": "phone", "number": to_number}],
            "from": {"type": "phone", "number": from_number},
            "ncco": ncco,
        }
        token = self._build_jwt()

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self.base_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"Vonage create call failed: {exc}") from exc

        call_sid = data.get("uuid") or data.get("conversation_uuid")
        if not call_sid:
            raise RuntimeError(f"Vonage create call returned no call id: {data}")

        status = data.get("status") or "started"
        logger.info(f"Vonage call initiated: UUID={call_sid}, status={status}")
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
        final_statuses = {
            "completed",
            "failed",
            "busy",
            "timeout",
            "rejected",
            "cancelled",
            "canceled",
            "unanswered",
        }
        token = self._build_jwt()
        last_status = None

        while elapsed < max_wait:
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(
                        f"{self.base_url}/{call_sid}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                response.raise_for_status()
                data = response.json()
                status = str(data.get("status") or "").lower()
                duration = int(data.get("duration") or 0)

                if status != last_status:
                    if status_callback:
                        try:
                            status_callback(status, duration, None)
                        except Exception as callback_error:
                            logger.warning(
                                f"Status callback error for {call_sid}: {callback_error}"
                            )
                    last_status = status

                if status in final_statuses:
                    return {
                        "status": status,
                        "duration": duration,
                        "answered_by": None,
                    }

                time.sleep(poll_interval)
                elapsed += poll_interval
            except Exception as exc:
                logger.warning(f"Vonage polling error for {call_sid}: {exc}")
                time.sleep(poll_interval)
                elapsed += poll_interval

        return {"status": "timeout", "duration": 0, "answered_by": None}
