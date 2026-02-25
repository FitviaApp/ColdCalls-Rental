"""
Dashboard Router - User home page and settings
"""
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, require_active_rental
from app.models import User, Campaign, CampaignStatus
from app.services.rental_service import get_active_rental
from app.services.user_telnyx_service import (
    has_user_telnyx_credentials,
    upsert_user_telnyx_credentials,
)
from app.services.user_twilio_service import (
    get_user_twilio_credentials,
    has_user_twilio_credentials,
    upsert_user_twilio_credentials,
)
from app.services.user_vonage_service import (
    has_user_vonage_credentials,
    upsert_user_vonage_credentials,
)
from app.services.user_voice_provider_service import has_any_user_voice_provider_credentials
from app.services.twilio_service import TwilioService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")

# E.164 phone number regex
E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Main dashboard page"""
    # Get recent campaigns
    campaigns = db.query(Campaign).filter(
        Campaign.user_id == user.id
    ).order_by(Campaign.created_at.desc()).limit(5).all()

    # Calculate stats
    all_campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).all()

    twilio_balance = None
    twilio_balance_currency = "USD"
    twilio_balance_error = None
    twilio_configured = has_user_twilio_credentials(db, user.id)
    if twilio_configured:
        try:
            account_sid, auth_token = get_user_twilio_credentials(db, user.id)
            twilio_service = TwilioService(account_sid=account_sid, auth_token=auth_token)
            balance_info = twilio_service.get_account_balance()
            if balance_info:
                twilio_balance = balance_info["balance"]
                twilio_balance_currency = balance_info["currency"]
            else:
                twilio_balance_error = "Unable to load Twilio balance right now."
        except Exception:
            twilio_balance_error = "Unable to load Twilio balance right now."

    stats = {
        "total_campaigns": len(all_campaigns),
        "active_campaigns": len([c for c in all_campaigns if c.status == CampaignStatus.RUNNING]),
        "total_calls": sum(c.processed_numbers for c in all_campaigns),
        "successful_calls": sum(c.successful_calls for c in all_campaigns),
        "total_spent": sum(c.total_cost for c in all_campaigns),
        "transfer_configured": bool(
            user.transfer_number and has_any_user_voice_provider_credentials(db, user.id)
        ),
        "rental_active": False,
        "twilio_configured": twilio_configured,
        "twilio_balance": twilio_balance,
        "twilio_balance_currency": twilio_balance_currency,
        "twilio_balance_error": twilio_balance_error,
    }
    active_rental = get_active_rental(db, user.id)
    stats["rental_active"] = bool(active_rental)

    return templates.TemplateResponse(
        "dashboard/index.html",
        {
            "request": request,
            "user": user,
            "campaigns": campaigns,
            "stats": stats,
            "active_rental": active_rental,
        }
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(require_active_rental),
    saved: bool = False,
    twilio_saved: bool = False,
    telnyx_saved: bool = False,
    vonage_saved: bool = False,
    db: Session = Depends(get_db)
):
    """User settings page"""
    return templates.TemplateResponse(
        "dashboard/settings.html",
        {
            "request": request,
            "user": user,
            "saved": saved,
            "twilio_saved": twilio_saved,
            "telnyx_saved": telnyx_saved,
            "vonage_saved": vonage_saved,
            "twilio_configured": has_user_twilio_credentials(db, user.id),
            "telnyx_configured": has_user_telnyx_credentials(db, user.id),
            "vonage_configured": has_user_vonage_credentials(db, user.id),
            "error": None
        }
    )


@router.post("/settings/transfer")
async def save_transfer_number(
    request: Request,
    transfer_number: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Save transfer number (3CX)"""
    # Validate transfer number
    transfer_number = transfer_number.strip()
    if not E164_PATTERN.match(transfer_number):
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Invalid transfer number. Use E.164 format (e.g., +15551234567)"
            },
            status_code=400
        )

    # Save transfer number
    user.transfer_number = transfer_number
    db.commit()

    return RedirectResponse(url="/dashboard/settings?saved=true", status_code=302)


@router.post("/settings/twilio")
async def save_twilio_credentials(
    request: Request,
    account_sid: str = Form(...),
    auth_token: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Save user's Twilio credentials."""
    account_sid = account_sid.strip()
    auth_token = auth_token.strip()

    if not account_sid.startswith("AC") or len(account_sid) != 34:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Invalid Twilio Account SID format."
            },
            status_code=400
        )

    if not auth_token:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Twilio Auth Token cannot be empty."
            },
            status_code=400
        )

    upsert_user_twilio_credentials(db, user.id, account_sid, auth_token)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?twilio_saved=true", status_code=302)


@router.post("/settings/telnyx")
async def save_telnyx_credentials(
    request: Request,
    api_key: str = Form(...),
    account_sid: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Save user's Telnyx credentials."""
    api_key = api_key.strip()
    account_sid = account_sid.strip()

    if not api_key:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Telnyx API Key cannot be empty."
            },
            status_code=400
        )
    if not account_sid:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Telnyx Account SID cannot be empty."
            },
            status_code=400
        )

    upsert_user_telnyx_credentials(db, user.id, api_key, account_sid)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?telnyx_saved=true", status_code=302)


@router.post("/settings/vonage")
async def save_vonage_credentials(
    request: Request,
    application_id: str = Form(...),
    private_key: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Save user's Vonage credentials."""
    application_id = application_id.strip()
    private_key = private_key.strip()

    if not application_id:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Vonage Application ID cannot be empty."
            },
            status_code=400
        )

    if "BEGIN" not in private_key:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            {
                "request": request,
                "user": user,
                "saved": False,
                "twilio_saved": False,
                "telnyx_saved": False,
                "vonage_saved": False,
                "twilio_configured": has_user_twilio_credentials(db, user.id),
                "telnyx_configured": has_user_telnyx_credentials(db, user.id),
                "vonage_configured": has_user_vonage_credentials(db, user.id),
                "error": "Vonage private key must be in PEM format."
            },
            status_code=400
        )

    upsert_user_vonage_credentials(db, user.id, application_id, private_key)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?vonage_saved=true", status_code=302)
