"""
SignalWire -> ElevenLabs SIP runtime for AI agent campaigns.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AIAgentRuntimeProvider, CampaignMode, CampaignNumber, VoiceProvider
from app.services.rental_service import has_active_rental
from app.services.signalwire_service import SignalWireService
from app.services.user_signalwire_service import get_user_signalwire_credentials

logger = logging.getLogger(__name__)
settings = get_settings()


class ElevenLabsSipRuntimeService:
    """Make SignalWire calls that hand audio/dialogue off to ElevenLabs via SIP."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        project_id, api_token, space_url = get_user_signalwire_credentials(db, user_id)
        if not project_id or not api_token or not space_url:
            raise ValueError("SignalWire credentials not configured")
        self.signalwire_service = SignalWireService(
            project_id=project_id,
            api_token=api_token,
            space_url=space_url,
        )

    def make_call(
        self,
        to_number: str,
        from_number: str,
        audio_url: str | None,
        transfer_number: str,
        campaign_id: int | None = None,
        press_1_to_talk_with_agent: bool = False,
        timeout: int = 60,
        metadata: dict | None = None,
    ) -> dict:
        del audio_url, transfer_number, press_1_to_talk_with_agent
        metadata = metadata or {}
        campaign_number_id = int(metadata.get("campaign_number_id") or 0)
        if not campaign_number_id:
            raise ValueError("ElevenLabs SIP runtime requires campaign_number_id metadata")

        answer_url = (
            f"{settings.BASE_URL.rstrip('/')}/api/ai-runtime/elevenlabs/twiml/{campaign_number_id}"
        )
        return self.signalwire_service.make_call(
            to_number=to_number,
            from_number=from_number,
            audio_url=None,
            transfer_number="",
            campaign_id=campaign_id,
            timeout=timeout,
            metadata=metadata,
            answer_url=answer_url,
            enable_machine_detection=False,
        )

    def poll_call_status(self, *args, **kwargs):
        return self.signalwire_service.poll_call_status(*args, **kwargs)


def build_elevenlabs_sip_twiml(campaign_number_id: int) -> str:
    db = SessionLocal()
    try:
        number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
        if not number or not number.campaign:
            return _hangup_twiml()

        campaign = number.campaign
        agent = campaign.ai_agent
        caller_id = campaign.caller_id
        if not has_active_rental(db, campaign.user_id):
            return _hangup_twiml()
        if (
            campaign.campaign_mode != CampaignMode.AI_AGENT
            or campaign.voice_provider != VoiceProvider.SIGNALWIRE
            or not agent
            or not agent.is_active
            or agent.runtime_provider != AIAgentRuntimeProvider.ELEVENLABS_AGENT
        ):
            return _hangup_twiml()
        if not caller_id or not caller_id.elevenlabs_phone_number_id:
            return _hangup_twiml()
        if not agent.external_agent_id:
            return _hangup_twiml()

        sip_uri = _build_sip_uri(
            external_agent_id=agent.external_agent_id,
            elevenlabs_phone_number_id=caller_id.elevenlabs_phone_number_id,
            campaign_number_id=campaign_number_id,
            campaign_id=campaign.id,
            caller_id_id=caller_id.id,
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{caller_id.phone_number}" timeout="45">
        <Sip>{sip_uri}</Sip>
    </Dial>
</Response>"""
    except Exception as exc:
        logger.error(
            "Failed to build ElevenLabs SIP TwiML for campaign_number_id=%s: %s",
            campaign_number_id,
            exc,
        )
        return _hangup_twiml()
    finally:
        db.close()


def _build_sip_uri(
    *,
    external_agent_id: str,
    elevenlabs_phone_number_id: str,
    campaign_number_id: int,
    campaign_id: int,
    caller_id_id: int,
) -> str:
    query = urlencode(
        {
            "X-ElevenLabs-Phone-Number-Id": elevenlabs_phone_number_id,
            "X-Campaign-Number-Id": campaign_number_id,
            "X-Campaign-Id": campaign_id,
            "X-Caller-Id-Id": caller_id_id,
        }
    )
    sip_domain = settings.ELEVENLABS_SIP_DOMAIN.strip()
    return f"sip:{external_agent_id}@{sip_domain}?{query}"


def _hangup_twiml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
