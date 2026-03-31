"""
Campaigns Router - CRUD and campaign management
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_active_rental
from app.models import (
    User, Campaign, CampaignNumber, CallerID, Country, Audio, AIAgent,
    AIAgentRuntimeProvider,
    CampaignStatus, CallStatus, VoiceProvider, VoxCallerIDVerificationStatus, CampaignMode
)
from app.services.ai_agent_service import list_user_ai_agents
from app.services.user_voice_provider_service import (
    get_user_voice_provider_status,
    has_any_user_voice_provider_credentials,
    has_user_elevenlabs_agent_runtime_credentials,
    has_user_legacy_ai_runtime_credentials,
    has_user_voice_provider_credentials,
    provider_supports_press_1,
    supported_voice_providers,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
templates = Jinja2Templates(directory="app/templates")
WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")
MIN_CONCURRENT_CALLS = 1
MAX_CONCURRENT_CALLS = 20

# E.164 phone number regex
E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


def validate_phone_number(number: str) -> Optional[str]:
    """Validate and clean phone number"""
    number = number.strip()
    if E164_PATTERN.match(number):
        return number
    return None


def _parse_campaign_numbers(numbers_raw: str) -> tuple[list[str], int]:
    """Parse pasted/uploaded numbers, keeping valid E.164 entries and counting invalid rows."""
    lines = (numbers_raw or "").strip().split('\n')
    valid_numbers: list[str] = []
    invalid_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if ',' in line:
            line = line.split(',')[0].strip()

        number = validate_phone_number(line)
        if number:
            valid_numbers.append(number)
        else:
            invalid_count += 1

    return valid_numbers, invalid_count


def _normalized_campaign_mode(value: str | None) -> str:
    return (value or CampaignMode.AUDIO.value).strip().lower()


def _runtime_provider_value(ai_agent) -> str:
    if not ai_agent:
        return AIAgentRuntimeProvider.LEGACY_OPENAI.value
    runtime_provider = getattr(ai_agent, "runtime_provider", AIAgentRuntimeProvider.LEGACY_OPENAI.value)
    if hasattr(runtime_provider, "value"):
        runtime_provider = runtime_provider.value
    return str(runtime_provider or AIAgentRuntimeProvider.LEGACY_OPENAI.value).strip().lower()


def _campaign_form_validation_error(
    *,
    voice_provider: str,
    campaign_mode: str,
    press_1_to_talk_with_agent: bool,
    max_concurrent_calls: int,
    provider_configured: bool,
) -> str | None:
    if voice_provider not in supported_voice_providers():
        return "Invalid voice provider selected."
    if not provider_configured:
        return f"Selected provider ({voice_provider}) is not configured in Settings."
    if campaign_mode == CampaignMode.AI_AGENT.value and voice_provider != VoiceProvider.SIGNALWIRE.value:
        return "AI agent campaigns currently require SignalWire as the voice provider."
    if press_1_to_talk_with_agent and not provider_supports_press_1(voice_provider):
        return "Press 1 flow is not available for the selected provider."
    if campaign_mode == CampaignMode.AI_AGENT.value and press_1_to_talk_with_agent:
        return "Press 1 flow is not available for AI agent campaigns."
    if not (MIN_CONCURRENT_CALLS <= max_concurrent_calls <= MAX_CONCURRENT_CALLS):
        return (
            f"Concurrent calls must be between {MIN_CONCURRENT_CALLS} "
            f"and {MAX_CONCURRENT_CALLS}."
        )
    return None


def _campaign_resource_validation_error(
    *,
    campaign_mode: str,
    voice_provider: str,
    caller_id,
    audio,
    ai_agent,
    selected_audio_id: int | None,
    selected_ai_agent_id: int | None,
    legacy_ai_runtime_configured: bool,
    elevenlabs_sip_runtime_configured: bool,
) -> str | None:
    if not caller_id:
        return "Invalid caller ID selection."
    if selected_audio_id is not None and not audio:
        return "Invalid audio selection."
    if campaign_mode == CampaignMode.AI_AGENT.value and not ai_agent:
        return "Select an active AI agent for AI agent campaigns."
    if campaign_mode == CampaignMode.AI_AGENT.value and ai_agent:
        runtime_provider = _runtime_provider_value(ai_agent)
        if runtime_provider == AIAgentRuntimeProvider.LEGACY_OPENAI.value and not legacy_ai_runtime_configured:
            return "Configure SignalWire, OpenAI, and ElevenLabs in Settings before using legacy AI runtime."
        if runtime_provider == AIAgentRuntimeProvider.ELEVENLABS_AGENT.value and not elevenlabs_sip_runtime_configured:
            return "Configure SignalWire and ElevenLabs in Settings before using ElevenLabs agent runtime."
        if runtime_provider == AIAgentRuntimeProvider.ELEVENLABS_AGENT.value:
            if not getattr(caller_id, "elevenlabs_phone_number_id", None):
                return "Selected Caller ID is missing ElevenLabs Phone Number ID."
            if not getattr(ai_agent, "external_agent_id", None):
                return "Selected AI agent is not synced to ElevenLabs yet."
    if campaign_mode == CampaignMode.AUDIO.value and selected_ai_agent_id is not None:
        return "AI agents can only be used with AI agent campaigns."
    if (
        voice_provider == VoiceProvider.VOXIMPLANT.value
        and caller_id.vox_verification_status != VoxCallerIDVerificationStatus.VERIFIED
    ):
        return "Selected Caller ID is not verified in Voximplant yet."
    return None


def _campaign_start_validation_error(
    *,
    campaign,
    user,
    provider_configured: bool,
    legacy_ai_runtime_configured: bool,
    elevenlabs_sip_runtime_configured: bool,
) -> str | None:
    provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)

    if not user.transfer_number:
        return "Please configure your Transfer Number (3CX) in Settings first"
    if campaign.campaign_mode == CampaignMode.AI_AGENT:
        if campaign.voice_provider != VoiceProvider.SIGNALWIRE:
            return "AI agent campaigns require SignalWire"
        if not campaign.ai_agent_id or not campaign.ai_agent or campaign.ai_agent.user_id != user.id:
            return "Campaign AI agent is missing or invalid"
        if not campaign.ai_agent.is_active:
            return "Selected AI agent is inactive"
        runtime_provider = _runtime_provider_value(campaign.ai_agent)
        if runtime_provider == AIAgentRuntimeProvider.LEGACY_OPENAI.value:
            if not legacy_ai_runtime_configured:
                return "Please configure SignalWire, OpenAI, and ElevenLabs credentials in Settings first"
        elif runtime_provider == AIAgentRuntimeProvider.ELEVENLABS_AGENT.value:
            if not elevenlabs_sip_runtime_configured:
                return "Please configure SignalWire and ElevenLabs credentials in Settings first"
            if not campaign.caller_id.elevenlabs_phone_number_id:
                return "Selected Caller ID is missing ElevenLabs Phone Number ID"
            if not campaign.ai_agent.external_agent_id:
                return "Selected AI agent is not synced to ElevenLabs yet"
        else:
            return "Unsupported AI runtime provider"
    if not provider_configured:
        return f"Please configure {provider.title()} credentials in Settings first"
    if campaign.press_1_to_talk_with_agent and not provider_supports_press_1(provider):
        return "Press 1 flow is not available for the selected provider"
    if (
        provider == VoiceProvider.VOXIMPLANT.value
        and campaign.caller_id.vox_verification_status != VoxCallerIDVerificationStatus.VERIFIED
    ):
        return "Selected Caller ID is not verified in Voximplant yet"
    if campaign.caller_id.user_id != user.id:
        return "Campaign resources ownership mismatch. Please create a new campaign with your own assets."
    if campaign.audio_id is not None and campaign.audio is None:
        return "Campaign audio not found. Please update the campaign audio."
    if campaign.audio and campaign.audio.user_id != user.id:
        return "Campaign resources ownership mismatch. Please create a new campaign with your own assets."
    if campaign.ai_agent and campaign.ai_agent.user_id != user.id:
        return "Campaign AI agent ownership mismatch. Please create a new campaign with your own AI agent."
    return None


def _load_create_dependencies(db: Session, user_id: int) -> dict:
    caller_ids = db.query(CallerID).filter(
        CallerID.is_active == True,
        CallerID.user_id == user_id
    ).all()
    audios = db.query(Audio).filter(
        Audio.is_active == True,
        Audio.user_id == user_id
    ).all()
    return {
        "caller_ids": caller_ids,
        "audios": audios,
        "ai_agents": list_user_ai_agents(db, user_id),
        "ai_runtime_configured": has_user_elevenlabs_agent_runtime_credentials(db, user_id),
        "legacy_ai_runtime_configured": has_user_legacy_ai_runtime_credentials(db, user_id),
        "elevenlabs_sip_runtime_configured": has_user_elevenlabs_agent_runtime_credentials(db, user_id),
        "voice_providers": get_user_voice_provider_status(db, user_id),
    }


def _create_form_data(
    *,
    name: str = "",
    caller_id_id: int | None = None,
    audio_id: str | None = None,
    ai_agent_id: str | None = None,
    campaign_mode: str = CampaignMode.AUDIO.value,
    voice_provider: str = VoiceProvider.TWILIO.value,
    press_1_to_talk_with_agent: bool = False,
    max_concurrent_calls: int = 1,
    numbers_text: str = "",
) -> dict:
    return {
        "name": name,
        "caller_id_id": str(caller_id_id) if caller_id_id is not None else "",
        "audio_id": str(audio_id or "").strip(),
        "ai_agent_id": str(ai_agent_id or "").strip(),
        "campaign_mode": campaign_mode,
        "voice_provider": voice_provider,
        "press_1_to_talk_with_agent": bool(press_1_to_talk_with_agent),
        "max_concurrent_calls": max_concurrent_calls,
        "numbers_text": numbers_text,
    }


def _render_create_campaign_error(
    request: Request,
    user: User,
    deps: dict,
    *,
    error: str,
    form_data: dict,
    status_code: int = 400,
):
    return templates.TemplateResponse(
        "campaigns/create.html",
        {
            "request": request,
            "user": user,
            **deps,
            "error": error,
            "form_data": form_data,
        },
        status_code=status_code,
    )


def _is_worker_online(max_age_seconds: int = 60) -> bool:
    """Check if the background worker heartbeat is recent."""
    try:
        if not WORKER_HEARTBEAT_FILE.exists():
            return False
        age_seconds = (datetime.utcnow().timestamp() - WORKER_HEARTBEAT_FILE.stat().st_mtime)
        return age_seconds <= max_age_seconds
    except Exception:
        return False


@router.get("", response_class=HTMLResponse)
async def list_campaigns(
    request: Request,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """List user's campaigns"""
    campaigns = db.query(Campaign).filter(
        Campaign.user_id == user.id
    ).order_by(Campaign.created_at.desc()).all()
    ai_campaign_ids = [
        campaign.id for campaign in campaigns if campaign.campaign_mode == CampaignMode.AI_AGENT
    ]
    ai_runtime_summary_by_campaign: dict[int, dict[str, int | bool]] = {}
    if ai_campaign_ids:
        rows = db.query(
            CampaignNumber.campaign_id,
            func.count(CampaignNumber.id),
            func.sum(case((CampaignNumber.ai_runtime_error.isnot(None), 1), else_=0)),
            func.sum(case((CampaignNumber.ai_handoff_reason.isnot(None), 1), else_=0)),
        ).filter(
            CampaignNumber.campaign_id.in_(ai_campaign_ids)
        ).group_by(CampaignNumber.campaign_id).all()
        for campaign_id, total_numbers, runtime_errors, handoffs in rows:
            ai_runtime_summary_by_campaign[int(campaign_id)] = {
                "total_numbers": int(total_numbers or 0),
                "runtime_errors": int(runtime_errors or 0),
                "handoffs": int(handoffs or 0),
                "policy_paused": False,
            }

        latest_errors = db.query(CampaignNumber).filter(
            CampaignNumber.campaign_id.in_(ai_campaign_ids),
            CampaignNumber.ai_runtime_error.isnot(None),
        ).order_by(CampaignNumber.processed_at.desc(), CampaignNumber.id.desc()).all()
        for number in latest_errors:
            summary = ai_runtime_summary_by_campaign.setdefault(
                number.campaign_id,
                {"total_numbers": 0, "runtime_errors": 0, "handoffs": 0, "policy_paused": False},
            )
            if "last_runtime_error" not in summary:
                summary["last_runtime_error"] = number.ai_runtime_error or ""
                summary["policy_paused"] = "policy" in (number.ai_runtime_error or "").lower()

    return templates.TemplateResponse(
        "campaigns/list.html",
        {
            "request": request,
            "user": user,
            "campaigns": campaigns,
            "ai_runtime_summary_by_campaign": ai_runtime_summary_by_campaign,
        }
    )


@router.get("/create", response_class=HTMLResponse)
async def create_campaign_page(
    request: Request,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Display campaign creation form"""
    deps = _load_create_dependencies(db, user.id)

    setup_error = None
    if not user.transfer_number:
        setup_error = "Please configure your Transfer Number (3CX) in Settings before creating a campaign."
    elif not has_any_user_voice_provider_credentials(db, user.id):
        setup_error = "Please configure at least one voice provider (Twilio, SignalWire, Telnyx, Vonage, or Voximplant) in Settings."

    return templates.TemplateResponse(
        "campaigns/create.html",
        {
            "request": request,
            "user": user,
            **deps,
            "error": setup_error,
            "form_data": _create_form_data(),
        }
    )


@router.post("/create")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    caller_id_id: int = Form(...),
    audio_id: Optional[str] = Form(default=None),
    ai_agent_id: Optional[str] = Form(default=None),
    campaign_mode: str = Form(default=CampaignMode.AUDIO.value),
    voice_provider: str = Form(default=VoiceProvider.TWILIO.value),
    press_1_to_talk_with_agent: bool = Form(False),
    max_concurrent_calls: int = Form(default=1),
    numbers_text: str = Form(default=""),
    numbers_file: Optional[UploadFile] = File(default=None),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Create a new campaign"""
    deps = _load_create_dependencies(db, user.id)
    form_data = _create_form_data(
        name=name,
        caller_id_id=caller_id_id,
        audio_id=audio_id,
        ai_agent_id=ai_agent_id,
        campaign_mode=campaign_mode,
        voice_provider=voice_provider,
        press_1_to_talk_with_agent=press_1_to_talk_with_agent,
        max_concurrent_calls=max_concurrent_calls,
        numbers_text=numbers_text,
    )

    # Check if user has transfer number configured
    if not user.transfer_number:
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error="Please configure your Transfer Number (3CX) in Settings before creating a campaign.",
            form_data=form_data,
        )
    if not has_any_user_voice_provider_credentials(db, user.id):
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error="Please configure at least one voice provider (Twilio, SignalWire, Telnyx, Vonage, or Voximplant) in Settings.",
            form_data=form_data,
        )

    campaign_mode = _normalized_campaign_mode(campaign_mode)
    form_data["campaign_mode"] = campaign_mode
    if campaign_mode not in {CampaignMode.AUDIO.value, CampaignMode.AI_AGENT.value}:
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error="Invalid campaign mode selected.",
            form_data=form_data,
        )

    voice_provider = (voice_provider or "").strip().lower()
    form_data["voice_provider"] = voice_provider
    form_error = _campaign_form_validation_error(
        voice_provider=voice_provider,
        campaign_mode=campaign_mode,
        press_1_to_talk_with_agent=press_1_to_talk_with_agent,
        max_concurrent_calls=max_concurrent_calls,
        provider_configured=has_user_voice_provider_credentials(db, user.id, voice_provider),
    )
    if form_error:
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error=form_error,
            form_data=form_data,
        )

    # Parse numbers from text or file
    numbers_raw = numbers_text

    if numbers_file and numbers_file.filename:
        content = await numbers_file.read()
        numbers_raw = content.decode('utf-8')

    # Parse and validate numbers
    valid_numbers, invalid_count = _parse_campaign_numbers(numbers_raw)

    if not valid_numbers:
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error=(
                "No valid phone numbers found. Numbers must be in E.164 format "
                f"(e.g., +5511999999999). {invalid_count} invalid numbers skipped."
            ),
            form_data=form_data,
            status_code=400,
        )

    # Verify foreign keys exist
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.is_active == True,
        CallerID.user_id == user.id
    ).first()
    selected_audio_id: Optional[int] = None
    selected_ai_agent_id: Optional[int] = None
    if campaign_mode == CampaignMode.AUDIO.value and audio_id is not None and str(audio_id).strip():
        try:
            selected_audio_id = int(str(audio_id).strip())
        except ValueError:
            return _render_create_campaign_error(
                request,
                user,
                deps,
                error="Invalid audio selection.",
                form_data=form_data,
            )

    if campaign_mode == CampaignMode.AI_AGENT.value and ai_agent_id is not None and str(ai_agent_id).strip():
        try:
            selected_ai_agent_id = int(str(ai_agent_id).strip())
        except ValueError:
            return _render_create_campaign_error(
                request,
                user,
                deps,
                error="Invalid AI agent selection.",
                form_data=form_data,
            )

    audio = None
    ai_agent = None
    if selected_audio_id is not None:
        audio = db.query(Audio).filter(
            Audio.id == selected_audio_id,
            Audio.is_active == True,
            Audio.user_id == user.id
        ).first()
    if selected_ai_agent_id is not None:
        ai_agent = db.query(AIAgent).filter(
            AIAgent.id == selected_ai_agent_id,
            AIAgent.user_id == user.id,
            AIAgent.is_active == True,
        ).first()

    resource_error = _campaign_resource_validation_error(
        campaign_mode=campaign_mode,
        voice_provider=voice_provider,
        caller_id=caller_id,
        audio=audio,
        ai_agent=ai_agent,
        selected_audio_id=selected_audio_id,
        selected_ai_agent_id=selected_ai_agent_id,
        legacy_ai_runtime_configured=has_user_legacy_ai_runtime_credentials(db, user.id),
        elevenlabs_sip_runtime_configured=has_user_elevenlabs_agent_runtime_credentials(db, user.id),
    )
    if resource_error:
        return _render_create_campaign_error(
            request,
            user,
            deps,
            error=resource_error,
            form_data=form_data,
        )
    if campaign_mode == CampaignMode.AUDIO.value and selected_audio_id is None and not press_1_to_talk_with_agent:
        # Direct transfer without audio remains valid.
        pass

    country_code = caller_id.country_code.strip().upper()[:5]
    country = db.query(Country).filter(
        Country.code == country_code
    ).first()
    if not country:
        country = Country(
            code=country_code,
            name=country_code,
            price_per_minute=0.0,
            is_active=True,
        )
        db.add(country)
        db.flush()

    # Create campaign
    campaign = Campaign(
        user_id=user.id,
        name=name,
        caller_id_id=caller_id_id,
        country_id=country.id,
        audio_id=audio.id if audio else None,
        ai_agent_id=ai_agent.id if ai_agent else None,
        campaign_mode=campaign_mode,
        voice_provider=voice_provider,
        press_1_to_talk_with_agent=press_1_to_talk_with_agent,
        max_concurrent_calls=max_concurrent_calls,
        status=CampaignStatus.DRAFT,
        total_numbers=len(valid_numbers)
    )
    db.add(campaign)
    db.flush()

    # Add numbers
    for number in valid_numbers:
        campaign_number = CampaignNumber(
            campaign_id=campaign.id,
            phone_number=number,
            status=CallStatus.PENDING
        )
        db.add(campaign_number)

    db.commit()

    return RedirectResponse(url=f"/campaigns/{campaign.id}", status_code=302)


@router.get("/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(
    request: Request,
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """View campaign details"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    numbers = db.query(CampaignNumber).filter(
        CampaignNumber.campaign_id == campaign_id
    ).order_by(CampaignNumber.id).all()
    ai_runtime_summary = None
    if campaign.campaign_mode == CampaignMode.AI_AGENT:
        ai_runtime_summary = {
            "total_numbers": len(numbers),
            "handoffs": sum(1 for n in numbers if n.ai_handoff_reason),
            "runtime_errors": sum(1 for n in numbers if n.ai_runtime_error),
            "lead_reprompts": sum(int(n.ai_no_input_turns or 0) for n in numbers),
            "avg_turns": float(
                db.query(func.coalesce(func.avg(CampaignNumber.ai_turn_count), 0)).filter(
                    CampaignNumber.campaign_id == campaign_id,
                    CampaignNumber.ai_turn_count.isnot(None),
                ).scalar() or 0.0
            ),
            "last_handoff_reason": next(
                (n.ai_handoff_reason for n in reversed(numbers) if n.ai_handoff_reason),
                None,
            ),
            "last_runtime_error": next(
                (n.ai_runtime_error for n in reversed(numbers) if n.ai_runtime_error),
                None,
            ),
            "policy_paused": any(
                "policy" in (n.ai_runtime_error or "").lower()
                for n in numbers
                if n.ai_runtime_error
            ) and campaign.status == CampaignStatus.PAUSED,
        }
    numbers_payload = [
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
            "processed_at": n.processed_at.isoformat() if n.processed_at else None,
        }
        for n in numbers
    ]

    return templates.TemplateResponse(
        "campaigns/detail.html",
        {
            "request": request,
            "user": user,
            "campaign": campaign,
            "numbers": numbers,
            "numbers_payload": numbers_payload,
            "ai_runtime_summary": ai_runtime_summary,
        }
    )


@router.post("/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Start a campaign"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status not in [CampaignStatus.DRAFT, CampaignStatus.PAUSED]:
        raise HTTPException(status_code=400, detail="Campaign cannot be started")

    if not _is_worker_online():
        raise HTTPException(
            status_code=503,
            detail="Worker is offline. Start/restart worker.py and try again."
        )

    provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)
    start_error = _campaign_start_validation_error(
        campaign=campaign,
        user=user,
        provider_configured=has_user_voice_provider_credentials(db, user.id, provider),
        legacy_ai_runtime_configured=has_user_legacy_ai_runtime_credentials(db, user.id),
        elevenlabs_sip_runtime_configured=has_user_elevenlabs_agent_runtime_credentials(db, user.id),
    )
    if start_error:
        raise HTTPException(status_code=400, detail=start_error)

    campaign.status = CampaignStatus.RUNNING
    campaign.started_at = datetime.utcnow()
    db.commit()

    return RedirectResponse(url=f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Pause a running campaign"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status != CampaignStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Campaign is not running")

    campaign.status = CampaignStatus.PAUSED
    db.commit()

    return RedirectResponse(url=f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Cancel a campaign"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign.status == CampaignStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Campaign is already completed")

    campaign.status = CampaignStatus.CANCELLED
    campaign.completed_at = datetime.utcnow()
    db.commit()

    return RedirectResponse(url=f"/campaigns/{campaign_id}", status_code=302)
