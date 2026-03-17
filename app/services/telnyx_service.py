"""
Telnyx Voice service (TeXML API based)
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Callable

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class TelnyxService:
    """Service for making Telnyx calls via TeXML REST API."""

    def __init__(self, api_key: str, account_sid: str):
        if not api_key or not account_sid:
            raise ValueError("Telnyx credentials not configured")

        self.api_key = api_key.strip()
        self.account_sid = account_sid.strip()
        self.base_url = "https://api.telnyx.com/v2/texml"

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
        del audio_url, transfer_number, timeout, metadata
        if press_1_to_talk_with_agent:
            raise ValueError("Press 1 flow is currently supported only for Twilio, SignalWire, and Voximplant campaigns")
        if campaign_id is None:
            raise ValueError("campaign_id is required for Telnyx campaigns")

        callback_base = settings.BASE_URL.rstrip("/")
        if not callback_base.startswith(("http://", "https://")):
            raise ValueError("BASE_URL must be a public http(s) URL for Telnyx callbacks")

        xml_url = f"{callback_base}/api/telnyx/texml/{campaign_id}"
        endpoint = f"{self.base_url}/Accounts/{self.account_sid}/Calls"
        payload = {
            "From": from_number,
            "To": to_number,
            "Url": xml_url,
            "Method": "POST",
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    endpoint,
                    data=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"Telnyx create call failed: {exc}") from exc

        call_sid = (
            data.get("sid")
            or data.get("call_sid")
            or (data.get("data") or {}).get("sid")
            or (data.get("data") or {}).get("call_sid")
        )
        if not call_sid:
            raise RuntimeError(f"Telnyx create call returned no call SID: {data}")

        status = (
            data.get("status")
            or (data.get("data") or {}).get("status")
            or "queued"
        )

        logger.info(f"Telnyx call initiated: SID={call_sid}, status={status}")
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
        final_statuses = {"completed", "failed", "busy", "no-answer", "canceled", "cancelled"}
        endpoint = f"{self.base_url}/Accounts/{self.account_sid}/Calls/{call_sid}"
        last_status = None

        while elapsed < max_wait:
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(
                        endpoint,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                response.raise_for_status()
                data = response.json()
                row = data.get("data") or data

                status = str(row.get("status") or "").lower()
                duration = int(row.get("duration") or 0)
                answered_by = row.get("answered_by")

                if status != last_status:
                    if status_callback:
                        try:
                            status_callback(status, duration, answered_by)
                        except Exception as callback_error:
                            logger.warning(
                                f"Status callback error for {call_sid}: {callback_error}"
                            )
                    last_status = status

                if status in final_statuses:
                    return {
                        "status": status,
                        "duration": duration,
                        "answered_by": answered_by,
                    }

                time.sleep(poll_interval)
                elapsed += poll_interval
            except Exception as exc:
                logger.warning(f"Telnyx polling error for {call_sid}: {exc}")
                time.sleep(poll_interval)
                elapsed += poll_interval

        return {"status": "timeout", "duration": 0, "answered_by": None}
