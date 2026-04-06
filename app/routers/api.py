"""
API Router - JSON endpoints for AJAX calls and cXML/TwiML
"""
import logging
import json
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.orm import Session
from redis.asyncio import Redis as AsyncRedis

from app.database import get_db
from app.config import get_settings
from app.dependencies import require_active_rental
from app.models import User, Campaign, CampaignNumber, CallerID, Country, Audio, CampaignStatus, VoiceProvider, CallStatus, CampaignMode
from app.schemas import DashboardStats, CampaignProgress, DropdownCallerID, DropdownCountry, DropdownAudio
from app.services.rental_service import has_active_rental
from app.services.ai_call_runtime_service import (
    build_ai_realtime_stream_twiml,
    build_ai_runtime_followup_twiml,
    build_ai_runtime_twiml,
    get_ai_runtime_audio,
    update_campaign_number_ai_observability,
)
from app.services.ai_realtime_event_bus import AsyncAIRealtimeEventBus, realtime_channel, AIRealtimeEvent
from app.services.ai_realtime_session_service import AIRealtimeSessionService
from app.services.voximplant_service import decode_voximplant_callback_token

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["api"])


# ============== cXML/TwiML Endpoint ==============
def _hangup_response() -> Response:
    twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return Response(content=twiml, media_type="application/xml")


def _build_transfer_block(campaign: Campaign, transfer_number: str) -> str:
    return f'''
    <Dial callerId="{campaign.caller_id.phone_number}" timeout="30">
        <Number>{transfer_number}</Number>
    </Dial>
'''


def _build_press_1_gather_block(base_url: str, campaign_id: int) -> str:
    return (
        f'<Gather input="dtmf" numDigits="1" timeout="8" '
        f'action="{base_url}/api/twiml/{campaign_id}/gather" method="POST" actionOnEmptyResult="true">'
        "<Say voice=\"alice\">Press 1 to talk with an agent.</Say>"
        "</Gather>"
    )


def _ai_runtime_hangup_response() -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
        media_type="application/xml",
    )


def _map_voximplant_callback_status(status: str) -> CallStatus:
    normalized = str(status or "").strip().lower()
    mapping = {
        "queued": CallStatus.QUEUED,
        "ringing": CallStatus.RINGING,
        "in_progress": CallStatus.IN_PROGRESS,
        "completed": CallStatus.COMPLETED,
        "failed": CallStatus.FAILED,
        "busy": CallStatus.BUSY,
        "no_answer": CallStatus.NO_ANSWER,
        "cancelled": CallStatus.CANCELLED,
        "canceled": CallStatus.CANCELLED,
    }
    return mapping.get(normalized, CallStatus.FAILED)


@router.post("/twiml/{campaign_id}")
@router.get("/twiml/{campaign_id}")
async def twiml_handler(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Dynamic cXML/TwiML endpoint for handling calls with machine detection.

    Twilio/SignalWire call this URL when the call is answered.
    - If human: Play audio, then transfer to 3CX number
    - If machine: Hang up

    Twilio/SignalWire sends AnsweredBy parameter:
    - human
    - machine_start, machine_end_beep, machine_end_silence, machine_end_other
    - fax
    - unknown
    """
    try:
        # Parse form data from Twilio/SignalWire (POST) or query params (GET)
        answered_by = ""
        if request.method == "POST":
            try:
                form_data = await request.form()
                answered_by = str(form_data.get("AnsweredBy", "")).strip()
            except Exception as exc:
                logger.warning(
                    "Failed to parse form payload for /api/twiml/%s: %s",
                    campaign_id,
                    exc,
                )
                answered_by = str(request.query_params.get("AnsweredBy", "")).strip()
        else:
            answered_by = str(request.query_params.get("AnsweredBy", "")).strip()

        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return _hangup_response()

        if not has_active_rental(db, campaign.user_id):
            return _hangup_response()
        if campaign.voice_provider not in {VoiceProvider.TWILIO, VoiceProvider.SIGNALWIRE}:
            return _hangup_response()

        transfer_number = campaign.user.transfer_number
        if not transfer_number:
            return _hangup_response()

        logger.info(f"cXML/TwiML request for campaign {campaign_id}: AnsweredBy={answered_by}")

        if answered_by.startswith("machine") or answered_by == "fax":
            logger.info(f"Campaign {campaign_id}: Machine detected ({answered_by}), hanging up")
            return _hangup_response()
        audio_url = campaign.audio.r2_url if campaign.audio else None

        if campaign.press_1_to_talk_with_agent:
            base_url = settings.BASE_URL.rstrip("/")
            logger.info(
                f"Campaign {campaign_id}: Human/unknown ({answered_by}), "
                "waiting for DTMF 1 before transfer"
            )
            if audio_url:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    {_build_press_1_gather_block(base_url, campaign_id)}
    <Hangup/>
</Response>'''
            else:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {_build_press_1_gather_block(base_url, campaign_id)}
    <Hangup/>
</Response>'''
        else:
            logger.info(
                f"Campaign {campaign_id}: Human/unknown ({answered_by}), "
                f"transferring to {transfer_number}"
            )
            if audio_url:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    {_build_transfer_block(campaign, transfer_number)}
</Response>'''
            else:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {_build_transfer_block(campaign, transfer_number)}
</Response>'''

        return Response(content=twiml, media_type="application/xml")
    except Exception as exc:
        logger.error("Unhandled /api/twiml/%s error: %s", campaign_id, exc, exc_info=True)
        return _hangup_response()


@router.post("/twiml/{campaign_id}/gather")
async def twiml_gather_handler(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    try:
        digits = ""
        try:
            form_data = await request.form()
            digits = str(form_data.get("Digits", "")).strip()
        except Exception as exc:
            logger.warning(
                "Failed to parse form payload for /api/twiml/%s/gather: %s",
                campaign_id,
                exc,
            )
            digits = str(request.query_params.get("Digits", "")).strip()

        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return _hangup_response()
        if not has_active_rental(db, campaign.user_id):
            return _hangup_response()
        if campaign.voice_provider not in {VoiceProvider.TWILIO, VoiceProvider.SIGNALWIRE}:
            return _hangup_response()

        transfer_number = campaign.user.transfer_number
        if not transfer_number:
            return _hangup_response()

        if digits == "1":
            logger.info(f"Campaign {campaign_id}: DTMF 1 received, transferring")
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {_build_transfer_block(campaign, transfer_number)}
</Response>'''
            return Response(content=twiml, media_type="application/xml")

        logger.info(f"Campaign {campaign_id}: Invalid/no DTMF ({digits}), hanging up")
        return _hangup_response()
    except Exception as exc:
        logger.error("Unhandled /api/twiml/%s/gather error: %s", campaign_id, exc, exc_info=True)
        return _hangup_response()


@router.post("/telnyx/texml/{campaign_id}")
@router.get("/telnyx/texml/{campaign_id}")
async def telnyx_texml_handler(
    campaign_id: int,
    db: Session = Depends(get_db)
):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return _hangup_response()
    if not has_active_rental(db, campaign.user_id):
        return _hangup_response()
    if campaign.voice_provider != VoiceProvider.TELNYX:
        return _hangup_response()

    transfer_number = campaign.user.transfer_number
    if not transfer_number:
        return _hangup_response()

    audio_url = campaign.audio.r2_url if campaign.audio else None
    if audio_url:
        # TeXML is TwiML-compatible for these basic verbs.
        texml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Dial callerId="{campaign.caller_id.phone_number}" timeout="30">
        <Number>{transfer_number}</Number>
    </Dial>
</Response>'''
    else:
        texml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{campaign.caller_id.phone_number}" timeout="30">
        <Number>{transfer_number}</Number>
    </Dial>
</Response>'''
    return Response(content=texml, media_type="application/xml")


@router.post("/voximplant/callback")
async def voximplant_callback(
    request: Request,
    db: Session = Depends(get_db)
):
    payload = await request.json()
    token = str(payload.get("token") or "").strip()
    decoded = decode_voximplant_callback_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid callback token")

    campaign_number_id = int(decoded.get("sub") or 0)
    number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
    if not number:
        raise HTTPException(status_code=404, detail="Campaign number not found")

    status = _map_voximplant_callback_status(payload.get("status"))
    duration = int(payload.get("duration") or 0)
    answered_by = payload.get("answered_by")
    error_message = payload.get("error_message")
    call_sid = payload.get("call_sid")

    number.status = status
    if call_sid:
        number.call_sid = str(call_sid)[:50]
    if duration >= 0:
        number.duration_seconds = duration
    if answered_by:
        number.answered_by = str(answered_by)[:50]
    if error_message:
        number.error_message = str(error_message)[:500]
    db.commit()


@router.get("/ai-runtime/twiml/{campaign_number_id}")
@router.post("/ai-runtime/twiml/{campaign_number_id}")
async def ai_runtime_twiml(campaign_number_id: int):
    twiml = build_ai_runtime_twiml(campaign_number_id)
    return Response(content=twiml, media_type="application/xml")


@router.post("/ai-runtime/twiml/{campaign_number_id}/gather")
async def ai_runtime_twiml_gather(
    campaign_number_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    number = db.query(CampaignNumber).filter(CampaignNumber.id == campaign_number_id).first()
    if not number or not number.campaign or number.campaign.campaign_mode != CampaignMode.AI_AGENT:
        return _ai_runtime_hangup_response()

    form_data = await request.form()
    speech_result = str(form_data.get("SpeechResult") or "").strip()
    digits = str(form_data.get("Digits") or "").strip()
    user_input = speech_result or digits

    twiml = build_ai_runtime_followup_twiml(campaign_number_id, user_input)
    return Response(content=twiml, media_type="application/xml")


@router.get("/ai-runtime/audio/{campaign_number_id}/{audio_token}")
async def ai_runtime_audio(campaign_number_id: int, audio_token: str):
    audio_bytes = get_ai_runtime_audio(campaign_number_id, audio_token)
    if not audio_bytes:
        raise HTTPException(status_code=404, detail="Audio not found")
    return Response(content=audio_bytes, media_type="audio/mpeg")


@router.get("/ai-realtime/twiml/{campaign_number_id}")
@router.post("/ai-realtime/twiml/{campaign_number_id}")
async def ai_realtime_twiml(campaign_number_id: int, token: str = ""):
    session_service = AIRealtimeSessionService()
    session = session_service.get_session(campaign_number_id)
    if not session:
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error="[media_bridge] Realtime session missing in Redis",
        )
        return _ai_runtime_hangup_response()

    token = str(token or "").strip()
    session_token = str(session.get("auth_token") or "").strip()
    if token and token != session_token:
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error="[media_bridge] Callback token mismatch; using session token fallback",
        )
    if not session_token:
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error="[media_bridge] Realtime session token missing",
        )
        return _ai_runtime_hangup_response()

    twiml = build_ai_realtime_stream_twiml(campaign_number_id, session_token)
    return Response(content=twiml, media_type="application/xml")


@router.get("/ai-realtime/session/{campaign_number_id}/health")
async def ai_realtime_session_health(campaign_number_id: int):
    session_service = AIRealtimeSessionService()
    session = session_service.get_session(campaign_number_id)
    if not session:
        raise HTTPException(status_code=404, detail="Realtime session not found")
    return {
        "campaign_number_id": campaign_number_id,
        "status": session.get("status") or "unknown",
        "turn_count": int(session.get("turn_count") or 0),
        "error_category": session.get("error_category") or "",
        "error_detail": session.get("error_detail") or "",
    }


@router.post("/ai-realtime/session/{campaign_number_id}/start")
async def ai_realtime_session_start(campaign_number_id: int):
    session_service = AIRealtimeSessionService()
    session = session_service.get_session(campaign_number_id)
    if not session:
        raise HTTPException(status_code=404, detail="Realtime session not found")
    session_service.update_session(campaign_number_id, status="starting")
    bus = AsyncAIRealtimeEventBus()
    try:
        await bus.publish(campaign_number_id, "session.started", {"source": "api_start"})
    finally:
        await bus.close()
    return {"ok": True}


@router.post("/ai-realtime/session/{campaign_number_id}/stop")
async def ai_realtime_session_stop(campaign_number_id: int):
    session_service = AIRealtimeSessionService()
    if not session_service.get_session(campaign_number_id):
        raise HTTPException(status_code=404, detail="Realtime session not found")
    session_service.end_session(campaign_number_id, status="stopped")
    bus = AsyncAIRealtimeEventBus()
    try:
        await bus.publish(campaign_number_id, "session.ended", {"source": "api_stop"})
    finally:
        await bus.close()
    return {"ok": True}


@router.websocket("/ai-realtime/ws/{campaign_number_id}")
@router.websocket("/ai-realtime/ws/{campaign_number_id}/{token}")
async def ai_realtime_ws(websocket: WebSocket, campaign_number_id: int, token: str = ""):
    token = token or str(websocket.query_params.get("token") or "")
    session_service = AIRealtimeSessionService()
    if not session_service.validate_auth_token(campaign_number_id, token):
        update_campaign_number_ai_observability(
            campaign_number_id,
            ai_runtime_error="[media_bridge] Realtime WS auth token rejected",
        )
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bus = AsyncAIRealtimeEventBus()
    redis = AsyncRedis.from_url(settings.REDIS_URL, decode_responses=True)
    channel = realtime_channel(campaign_number_id)
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(channel)
    stop_event = asyncio.Event()
    stream_sid = ""
    pending_outbound_audio: list[str] = []

    async def forward_outbound_media():
        nonlocal stream_sid
        while not stop_event.is_set():
            message = await pubsub.get_message(timeout=1.0)
            if not message:
                await asyncio.sleep(0.05)
                continue
            raw = str(message.get("data") or "")
            if not raw:
                continue
            event = AIRealtimeEvent.from_json(raw)
            if event.event == "media.outbound":
                audio = str((event.payload or {}).get("audio") or "")
                if not audio:
                    continue
                if not stream_sid:
                    pending_outbound_audio.append(audio)
                    # Keep memory bounded if stream start is delayed.
                    if len(pending_outbound_audio) > 200:
                        pending_outbound_audio = pending_outbound_audio[-200:]
                    continue
                await websocket.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio},
                        }
                    )
                )
            elif event.event == "assistant.response":
                text = str((event.payload or {}).get("text") or "")
                if text:
                    update_campaign_number_ai_observability(
                        campaign_number_id,
                        ai_last_assistant_text=text[:500],
                    )
            elif event.event == "tool.transfer_call":
                reason = str((event.payload or {}).get("reason") or "")
                update_campaign_number_ai_observability(
                    campaign_number_id,
                    ai_handoff_reason=reason[:500] if reason else None,
                )
            elif event.event == "session.error":
                category = str((event.payload or {}).get("category") or "").strip()
                detail = str((event.payload or {}).get("detail") or "").strip()
                error_message = f"[{category}] {detail}" if category else detail
                update_campaign_number_ai_observability(
                    campaign_number_id,
                    ai_runtime_error=error_message[:500],
                )

    outbound_task = asyncio.create_task(forward_outbound_media())

    try:
        while True:
            raw_message = await websocket.receive_text()
            data = json.loads(raw_message or "{}")
            event_type = str(data.get("event") or "").lower()
            if event_type == "start":
                start = data.get("start") or {}
                stream_sid = str(start.get("streamSid") or data.get("streamSid") or "")
                if stream_sid:
                    session_service.set_stream_sid(campaign_number_id, stream_sid)
                    if pending_outbound_audio:
                        for audio in pending_outbound_audio:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": audio},
                                    }
                                )
                            )
                        pending_outbound_audio.clear()
                session_service.update_session(campaign_number_id, status="streaming")
                await bus.publish(
                    campaign_number_id,
                    "session.started",
                    {"source": "provider_ws", "stream_sid": stream_sid},
                )
            elif event_type == "media":
                media = data.get("media") or {}
                payload = str(media.get("payload") or "")
                if payload:
                    await bus.publish(
                        campaign_number_id,
                        "media.inbound",
                        {"audio": payload},
                    )
            elif event_type == "stop":
                await bus.publish(
                    campaign_number_id,
                    "session.ended",
                    {"source": "provider_ws_stop"},
                )
                session_service.end_session(campaign_number_id, status="ended")
                break
    except WebSocketDisconnect:
        session_service.end_session(campaign_number_id, status="disconnected")
        await bus.publish(campaign_number_id, "session.ended", {"source": "ws_disconnect"})
    finally:
        stop_event.set()
        outbound_task.cancel()
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            pass
        try:
            await redis.aclose()
        except Exception:
            pass
        await bus.close()


@router.get("/stats", response_model=DashboardStats)
async def get_stats(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get dashboard statistics"""
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).all()

    return DashboardStats(
        total_campaigns=len(campaigns),
        active_campaigns=len([c for c in campaigns if c.status == CampaignStatus.RUNNING]),
        total_calls=sum(c.processed_numbers for c in campaigns),
        successful_calls=sum(c.successful_calls for c in campaigns),
        total_spent=sum(c.total_cost for c in campaigns)
    )


@router.get("/campaigns/{campaign_id}/progress", response_model=CampaignProgress)
async def get_campaign_progress(
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get campaign progress for real-time updates"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return CampaignProgress(
        status=campaign.status,
        total=campaign.total_numbers,
        processed=campaign.processed_numbers,
        successful=campaign.successful_calls,
        failed=campaign.failed_calls,
        cost=campaign.total_cost,
        progress_percent=campaign.progress_percent
    )


@router.get("/campaigns/{campaign_id}/numbers")
async def get_campaign_numbers(
    campaign_id: int,
    page: int = 1,
    per_page: int = 50,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get paginated list of campaign numbers"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    offset = (page - 1) * per_page
    numbers = db.query(CampaignNumber).filter(
        CampaignNumber.campaign_id == campaign_id
    ).order_by(CampaignNumber.id).offset(offset).limit(per_page).all()

    total = db.query(CampaignNumber).filter(
        CampaignNumber.campaign_id == campaign_id
    ).count()

    return {
        "numbers": [
            {
                "id": n.id,
                "phone_number": n.phone_number,
                "status": n.status.value,
                "duration_seconds": n.duration_seconds,
                "cost": n.cost,
                "answered_by": n.answered_by,
                "ai_turn_count": n.ai_turn_count,
                "ai_no_input_turns": n.ai_no_input_turns,
                "ai_last_user_input": n.ai_last_user_input,
                "ai_last_assistant_text": n.ai_last_assistant_text,
                "ai_handoff_reason": n.ai_handoff_reason,
                "ai_runtime_error": n.ai_runtime_error,
                "processed_at": n.processed_at.isoformat() if n.processed_at else None
            }
            for n in numbers
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    }


# ============== Dropdown Data ==============

@router.get("/data/caller-ids", response_model=list[DropdownCallerID])
async def get_caller_ids(
    country: str = None,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active caller IDs, optionally filtered by country"""
    query = db.query(CallerID).filter(
        CallerID.is_active == True,
        CallerID.user_id == user.id
    )

    if country:
        query = query.filter(CallerID.country_code == country.upper())

    return query.order_by(CallerID.phone_number).all()


@router.get("/data/countries", response_model=list[DropdownCountry])
async def get_countries(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active countries"""
    return db.query(Country).filter(
        Country.is_active == True
    ).order_by(Country.name).all()


@router.get("/data/audios", response_model=list[DropdownAudio])
async def get_audios(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active audios"""
    return db.query(Audio).filter(
        Audio.is_active == True,
        Audio.user_id == user.id
    ).order_by(Audio.name).all()
