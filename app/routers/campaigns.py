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
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_active_rental
from app.models import (
    User, Campaign, CampaignNumber, CallerID, Country, Audio,
    CampaignStatus, CallStatus, VoiceProvider
)
from app.services.user_voice_provider_service import (
    get_user_voice_provider_status,
    has_any_user_voice_provider_credentials,
    has_user_voice_provider_credentials,
    supported_voice_providers,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
templates = Jinja2Templates(directory="app/templates")
WORKER_HEARTBEAT_FILE = Path("/tmp/coldcalls_worker_heartbeat")

# E.164 phone number regex
E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


def validate_phone_number(number: str) -> Optional[str]:
    """Validate and clean phone number"""
    number = number.strip()
    if E164_PATTERN.match(number):
        return number
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
        "voice_providers": get_user_voice_provider_status(db, user_id),
    }


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

    return templates.TemplateResponse(
        "campaigns/list.html",
        {
            "request": request,
            "user": user,
            "campaigns": campaigns
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
        setup_error = "Please configure at least one voice provider (Twilio, Telnyx, or Vonage) in Settings."

    return templates.TemplateResponse(
        "campaigns/create.html",
        {
            "request": request,
            "user": user,
            **deps,
            "error": setup_error
        }
    )


@router.post("/create")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    caller_id_id: int = Form(...),
    audio_id: int = Form(...),
    voice_provider: str = Form(default=VoiceProvider.TWILIO.value),
    press_1_to_talk_with_agent: bool = Form(False),
    numbers_text: str = Form(default=""),
    numbers_file: Optional[UploadFile] = File(default=None),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Create a new campaign"""
    deps = _load_create_dependencies(db, user.id)

    # Check if user has transfer number configured
    if not user.transfer_number:
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": "Please configure your Transfer Number (3CX) in Settings before creating a campaign."
            },
            status_code=400
        )
    if not has_any_user_voice_provider_credentials(db, user.id):
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": "Please configure at least one voice provider (Twilio, Telnyx, or Vonage) in Settings."
            },
            status_code=400
        )

    voice_provider = (voice_provider or "").strip().lower()
    if voice_provider not in supported_voice_providers():
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": "Invalid voice provider selected."
            },
            status_code=400
        )
    if not has_user_voice_provider_credentials(db, user.id, voice_provider):
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": f"Selected provider ({voice_provider}) is not configured in Settings."
            },
            status_code=400
        )
    if press_1_to_talk_with_agent and voice_provider != VoiceProvider.TWILIO.value:
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": "Press 1 flow is currently available only with Twilio."
            },
            status_code=400
        )

    # Parse numbers from text or file
    numbers_raw = numbers_text

    if numbers_file and numbers_file.filename:
        content = await numbers_file.read()
        numbers_raw = content.decode('utf-8')

    # Parse and validate numbers
    lines = numbers_raw.strip().split('\n')
    valid_numbers = []
    invalid_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Handle CSV format (take first column)
        if ',' in line:
            line = line.split(',')[0].strip()

        number = validate_phone_number(line)
        if number:
            valid_numbers.append(number)
        else:
            invalid_count += 1

    if not valid_numbers:
        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                **deps,
                "error": f"No valid phone numbers found. Numbers must be in E.164 format (e.g., +5511999999999). {invalid_count} invalid numbers skipped."
            },
            status_code=400
        )

    # Verify foreign keys exist
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.is_active == True,
        CallerID.user_id == user.id
    ).first()
    audio = db.query(Audio).filter(
        Audio.id == audio_id,
        Audio.is_active == True,
        Audio.user_id == user.id
    ).first()

    if not caller_id or not audio:
        raise HTTPException(status_code=400, detail="Invalid caller ID or audio selection")

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
        audio_id=audio_id,
        voice_provider=voice_provider,
        press_1_to_talk_with_agent=press_1_to_talk_with_agent,
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

    return templates.TemplateResponse(
        "campaigns/detail.html",
        {
            "request": request,
            "user": user,
            "campaign": campaign,
            "numbers": numbers
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

    if not user.transfer_number:
        raise HTTPException(status_code=400, detail="Please configure your Transfer Number (3CX) in Settings first")

    provider = campaign.voice_provider.value if hasattr(campaign.voice_provider, "value") else str(campaign.voice_provider)
    if not has_user_voice_provider_credentials(db, user.id, provider):
        raise HTTPException(
            status_code=400,
            detail=f"Please configure {provider.title()} credentials in Settings first"
        )
    if campaign.press_1_to_talk_with_agent and provider != VoiceProvider.TWILIO.value:
        raise HTTPException(
            status_code=400,
            detail="Press 1 flow is currently available only with Twilio campaigns"
        )

    if campaign.caller_id.user_id != user.id or campaign.audio.user_id != user.id:
        raise HTTPException(
            status_code=400,
            detail="Campaign resources ownership mismatch. Please create a new campaign with your own assets."
        )

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
