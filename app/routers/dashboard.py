"""
Dashboard Router - User home page and settings
"""
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
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
from app.services.user_voximplant_service import (
    get_user_voximplant_credentials,
    has_user_voximplant_credentials,
    upsert_user_voximplant_credentials,
)
from app.services.user_voice_provider_service import has_any_user_voice_provider_credentials
from app.services.twilio_service import TwilioService
from app.services.voximplant_management_service import ensure_user_voximplant_resources

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")

# E.164 phone number regex
E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


def _settings_context(
    request: Request,
    user: User,
    db: Session,
    *,
    saved: bool = False,
    twilio_saved: bool = False,
    telnyx_saved: bool = False,
    vonage_saved: bool = False,
    voximplant_saved: bool = False,
    voximplant_provisioned: bool = False,
    password_saved: bool = False,
    error: str | None = None,
) -> dict:
    voximplant_credentials = get_user_voximplant_credentials(db, user.id)
    return {
        "request": request,
        "user": user,
        "saved": saved,
        "twilio_saved": twilio_saved,
        "telnyx_saved": telnyx_saved,
        "vonage_saved": vonage_saved,
        "voximplant_saved": voximplant_saved,
        "voximplant_provisioned": voximplant_provisioned,
        "password_saved": password_saved,
        "twilio_configured": has_user_twilio_credentials(db, user.id),
        "telnyx_configured": has_user_telnyx_credentials(db, user.id),
        "vonage_configured": has_user_vonage_credentials(db, user.id),
        "voximplant_configured": has_user_voximplant_credentials(db, user.id),
        "voximplant_status": voximplant_credentials.provision_status if voximplant_credentials else None,
        "voximplant_error": voximplant_credentials.provision_error if voximplant_credentials else None,
        "error": error,
    }


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
        "voximplant_configured": has_user_voximplant_credentials(db, user.id),
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
    voximplant_saved: bool = False,
    voximplant_provisioned: bool = False,
    password_saved: bool = False,
    db: Session = Depends(get_db)
):
    """User settings page"""
    return templates.TemplateResponse(
        "dashboard/settings.html",
        _settings_context(
            request,
            user,
            db,
            saved=saved,
            twilio_saved=twilio_saved,
            telnyx_saved=telnyx_saved,
            vonage_saved=vonage_saved,
            voximplant_saved=voximplant_saved,
            voximplant_provisioned=voximplant_provisioned,
            password_saved=password_saved,
        ),
    )


@router.post("/settings/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Allow authenticated users (including admins) to change their password."""
    current_password = current_password.strip()
    new_password = new_password.strip()
    confirm_password = confirm_password.strip()

    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Current password is incorrect."),
            status_code=400
        )

    if len(new_password) < 6:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="New password must be at least 6 characters."),
            status_code=400
        )

    if new_password != confirm_password:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="New password and confirmation do not match."),
            status_code=400
        )

    if verify_password(new_password, user.password_hash):
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="New password must be different from current password."),
            status_code=400
        )

    user.password_hash = hash_password(new_password)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?password_saved=true", status_code=302)


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
            _settings_context(
                request,
                user,
                db,
                error="Invalid transfer number. Use E.164 format (e.g., +15551234567)",
            ),
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
            _settings_context(request, user, db, error="Invalid Twilio Account SID format."),
            status_code=400
        )

    if not auth_token:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Twilio Auth Token cannot be empty."),
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
            _settings_context(request, user, db, error="Telnyx API Key cannot be empty."),
            status_code=400
        )
    if not account_sid:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Telnyx Account SID cannot be empty."),
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
            _settings_context(request, user, db, error="Vonage Application ID cannot be empty."),
            status_code=400
        )

    if "BEGIN" not in private_key:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Vonage private key must be in PEM format."),
            status_code=400
        )

    upsert_user_vonage_credentials(db, user.id, application_id, private_key)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?vonage_saved=true", status_code=302)


@router.post("/settings/voximplant")
async def save_voximplant_credentials(
    request: Request,
    account_id: str = Form(...),
    application_id: str = Form(default=""),
    service_account_email: str = Form(...),
    key_id: str = Form(...),
    private_key: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    account_id = account_id.strip()
    application_id = application_id.strip()
    service_account_email = service_account_email.strip()
    key_id = key_id.strip()
    private_key = private_key.strip()

    if not account_id.isdigit():
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Voximplant Account ID must be numeric."),
            status_code=400,
        )
    if "@" not in service_account_email:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Voximplant service account email is invalid."),
            status_code=400,
        )
    if not key_id:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Voximplant Key ID cannot be empty."),
            status_code=400,
        )
    if "BEGIN" not in private_key:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Voximplant private key must be in PEM format."),
            status_code=400,
        )

    row = upsert_user_voximplant_credentials(
        db,
        user.id,
        account_id=account_id,
        application_id=application_id,
        service_account_email=service_account_email,
        key_id=key_id,
        private_key=private_key,
    )
    db.flush()

    credentials = get_user_voximplant_credentials(db, user.id)
    try:
        ensure_user_voximplant_resources(db, user, row, credentials)
        db.commit()
    except Exception as exc:
        row.provision_status = "failed"
        row.provision_error = str(exc)[:500]
        db.commit()
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(
                request,
                user,
                db,
                error=f"Voximplant provisioning failed: {exc}",
            ),
            status_code=400,
        )

    return RedirectResponse(
        url="/dashboard/settings?voximplant_saved=true&voximplant_provisioned=true",
        status_code=302,
    )


@router.post("/settings/voximplant/reprovision")
async def reprovision_voximplant(
    request: Request,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    row = user.voximplant_credentials
    credentials = get_user_voximplant_credentials(db, user.id)
    if not row or not credentials:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="Configure Voximplant credentials first."),
            status_code=400,
        )
    try:
        ensure_user_voximplant_resources(db, user, row, credentials)
        db.commit()
    except Exception as exc:
        row.provision_status = "failed"
        row.provision_error = str(exc)[:500]
        db.commit()
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error=f"Voximplant reprovision failed: {exc}"),
            status_code=400,
        )

    return RedirectResponse(
        url="/dashboard/settings?voximplant_provisioned=true",
        status_code=302,
    )
