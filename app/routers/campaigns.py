"""
Campaigns Router - CRUD and campaign management
"""
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_twilio_configured
from app.models import User, Campaign, CampaignNumber, CallerID, Country, Audio, CampaignStatus, CallStatus

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
templates = Jinja2Templates(directory="app/templates")

# E.164 phone number regex
E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


def validate_phone_number(number: str) -> Optional[str]:
    """Validate and clean phone number"""
    number = number.strip()
    if E164_PATTERN.match(number):
        return number
    return None


@router.get("", response_class=HTMLResponse)
async def list_campaigns(
    request: Request,
    user: User = Depends(get_current_user),
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
    user: User = Depends(require_twilio_configured),
    db: Session = Depends(get_db)
):
    """Display campaign creation form"""
    caller_ids = db.query(CallerID).filter(CallerID.is_active == True).all()
    countries = db.query(Country).filter(Country.is_active == True).all()
    audios = db.query(Audio).filter(Audio.is_active == True).all()

    return templates.TemplateResponse(
        "campaigns/create.html",
        {
            "request": request,
            "user": user,
            "caller_ids": caller_ids,
            "countries": countries,
            "audios": audios,
            "error": None
        }
    )


@router.post("/create")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    caller_id_id: int = Form(...),
    country_id: int = Form(...),
    audio_id: int = Form(...),
    numbers_text: str = Form(default=""),
    numbers_file: Optional[UploadFile] = File(default=None),
    user: User = Depends(require_twilio_configured),
    db: Session = Depends(get_db)
):
    """Create a new campaign"""
    # Check if user has transfer number configured
    if not user.transfer_number:
        caller_ids = db.query(CallerID).filter(CallerID.is_active == True).all()
        countries = db.query(Country).filter(Country.is_active == True).all()
        audios = db.query(Audio).filter(Audio.is_active == True).all()

        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                "caller_ids": caller_ids,
                "countries": countries,
                "audios": audios,
                "error": "Please configure your Transfer Number (3CX) in Settings before creating a campaign."
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
        caller_ids = db.query(CallerID).filter(CallerID.is_active == True).all()
        countries = db.query(Country).filter(Country.is_active == True).all()
        audios = db.query(Audio).filter(Audio.is_active == True).all()

        return templates.TemplateResponse(
            "campaigns/create.html",
            {
                "request": request,
                "user": user,
                "caller_ids": caller_ids,
                "countries": countries,
                "audios": audios,
                "error": f"No valid phone numbers found. Numbers must be in E.164 format (e.g., +5511999999999). {invalid_count} invalid numbers skipped."
            },
            status_code=400
        )

    # Verify foreign keys exist
    caller_id = db.query(CallerID).filter(CallerID.id == caller_id_id, CallerID.is_active == True).first()
    country = db.query(Country).filter(Country.id == country_id, Country.is_active == True).first()
    audio = db.query(Audio).filter(Audio.id == audio_id, Audio.is_active == True).first()

    if not caller_id or not country or not audio:
        raise HTTPException(status_code=400, detail="Invalid caller ID, country, or audio selection")

    # Create campaign
    campaign = Campaign(
        user_id=user.id,
        name=name,
        caller_id_id=caller_id_id,
        country_id=country_id,
        audio_id=audio_id,
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(require_twilio_configured),
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

    # Estimate cost and check credits
    country = campaign.country
    estimated_cost = campaign.total_numbers * country.price_per_minute * 2  # Assume 2 min avg

    if user.credits < estimated_cost:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient credits. Estimated cost: ${estimated_cost:.2f}, Available: ${user.credits:.2f}"
        )

    # Reserve credits
    campaign.reserved_credits = estimated_cost
    campaign.status = CampaignStatus.RUNNING
    campaign.started_at = datetime.utcnow()
    db.commit()

    return RedirectResponse(url=f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
