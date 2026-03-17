"""
Voximplant runtime service.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.auth import create_access_token, decode_access_token
from app.config import get_settings
from app.database import SessionLocal
from app.models import CampaignNumber
from app.services.user_voximplant_service import VoximplantCredentials
from app.services.voximplant_management_service import VoximplantManagementService

logger = logging.getLogger(__name__)
settings = get_settings()


class VoximplantService:
    def __init__(self, db: Session, credentials: VoximplantCredentials):
        if not credentials or not credentials.vox_rule_id:
            raise ValueError("Voximplant resources are not provisioned")
        self.db = db
        self.credentials = credentials
        self.management = VoximplantManagementService(credentials)

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
        del timeout
        metadata = metadata or {}
        campaign_number_id = int(metadata.get("campaign_number_id") or 0)
        if not campaign_number_id:
            raise ValueError("campaign_number_id is required for Voximplant calls")

        callback_token = create_access_token(
            {
                "sub": str(campaign_number_id),
                "campaign_id": campaign_id,
                "provider": "voximplant_callback",
            },
            expires_delta=timedelta(hours=2),
        )
        callback_url = f"{settings.BASE_URL.rstrip('/')}/api/voximplant/callback"

        result = self.management.start_scenario(
            self.credentials.vox_rule_id,
            {
                "campaign_id": campaign_id,
                "campaign_number_id": campaign_number_id,
                "to_number": to_number,
                "caller_id": from_number,
                "audio_url": audio_url,
                "transfer_number": transfer_number,
                "press_1_to_talk_with_agent": bool(press_1_to_talk_with_agent),
                "status_callback_url": callback_url,
                "callback_token": callback_token,
            },
        )
        call_sid = (
            result.get("session_id")
            or result.get("session_access_url")
            or result.get("request_id")
            or f"voximplant-{campaign_number_id}"
        )
        return {"call_sid": str(call_sid), "status": "queued"}

    def poll_call_status(
        self,
        call_sid: str,
        max_wait: int = 70,
        poll_interval: int = 2,
        status_callback: Optional[Callable[[str, int, Optional[str]], None]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        del call_sid
        metadata = metadata or {}
        campaign_number_id = int(metadata.get("campaign_number_id") or 0)
        if not campaign_number_id:
            raise ValueError("campaign_number_id is required for Voximplant polling")

        elapsed = 0
        last_status = None
        final_statuses = {"completed", "failed", "busy", "no_answer", "cancelled", "canceled"}

        while elapsed < max_wait:
            callback_db = SessionLocal()
            try:
                number = callback_db.query(CampaignNumber).filter(
                    CampaignNumber.id == campaign_number_id
                ).first()
                if not number:
                    return {"status": "failed", "duration": 0, "answered_by": None}

                current_status = (
                    number.status.value if hasattr(number.status, "value") else str(number.status)
                )
                duration = int(number.duration_seconds or 0)
                answered_by = number.answered_by

                if current_status != last_status and status_callback:
                    status_callback(current_status, duration, answered_by)
                last_status = current_status

                if current_status in final_statuses:
                    return {
                        "status": current_status,
                        "duration": duration,
                        "answered_by": answered_by,
                    }
            finally:
                callback_db.close()

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("Voximplant polling timeout for campaign_number_id=%s", campaign_number_id)
        return {"status": "timeout", "duration": 0, "answered_by": None}


def decode_voximplant_callback_token(token: str) -> dict | None:
    payload = decode_access_token(token)
    if not payload:
        return None
    if payload.get("provider") != "voximplant_callback":
        return None
    return payload
