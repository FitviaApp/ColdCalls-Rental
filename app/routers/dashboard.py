"""
Dashboard Router - User home page and settings
"""
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
from app.database import get_db
from app.dependencies import get_current_user, require_active_rental
from app.models import User, Campaign, CampaignNumber, CampaignStatus, CampaignMode, AIAgent
from app.templating import Jinja2Templates
from app.services.rental_service import get_active_rental
from app.services.user_telnyx_service import (
    has_user_telnyx_credentials,
    upsert_user_telnyx_credentials,
)
from app.services.user_openai_service import (
    has_user_openai_credentials,
    upsert_user_openai_credentials,
)
from app.services.user_elevenlabs_service import (
    has_user_elevenlabs_credentials,
    upsert_user_elevenlabs_credentials,
)
from app.services.user_signalwire_service import (
    has_user_signalwire_credentials,
    upsert_user_signalwire_credentials,
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
from app.services.user_voice_provider_service import (
    has_user_ai_runtime_credentials,
    has_user_elevenlabs_agent_runtime_credentials,
    has_user_legacy_ai_runtime_credentials,
)
from app.services.twilio_service import TwilioService
from app.services.voximplant_management_service import ensure_user_voximplant_resources
from app.services.elevenlabs_agent_sync_service import check_elevenlabs_credentials

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
    signalwire_saved: bool = False,
    telnyx_saved: bool = False,
    vonage_saved: bool = False,
    voximplant_saved: bool = False,
    openai_saved: bool = False,
    elevenlabs_saved: bool = False,
    elevenlabs_checked: bool = False,
    elevenlabs_check_error: str | None = None,
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
        "signalwire_saved": signalwire_saved,
        "telnyx_saved": telnyx_saved,
        "vonage_saved": vonage_saved,
        "voximplant_saved": voximplant_saved,
        "openai_saved": openai_saved,
        "elevenlabs_saved": elevenlabs_saved,
        "elevenlabs_checked": elevenlabs_checked,
        "elevenlabs_check_error": elevenlabs_check_error,
        "voximplant_provisioned": voximplant_provisioned,
        "password_saved": password_saved,
        "twilio_configured": has_user_twilio_credentials(db, user.id),
        "signalwire_configured": has_user_signalwire_credentials(db, user.id),
        "telnyx_configured": has_user_telnyx_credentials(db, user.id),
        "vonage_configured": has_user_vonage_credentials(db, user.id),
        "voximplant_configured": has_user_voximplant_credentials(db, user.id),
        "openai_configured": has_user_openai_credentials(db, user.id),
        "elevenlabs_configured": has_user_elevenlabs_credentials(db, user.id),
        "elevenlabs_sip_runtime_configured": has_user_elevenlabs_agent_runtime_credentials(db, user.id),
        "legacy_ai_runtime_configured": has_user_legacy_ai_runtime_credentials(db, user.id),
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
    ai_campaigns = [c for c in all_campaigns if c.campaign_mode == CampaignMode.AI_AGENT]
    ai_campaign_ids = [c.id for c in ai_campaigns]
    ai_runtime_totals = {
        "numbers": 0,
        "handoffs": 0,
        "errors": 0,
        "silent_turns": 0,
        "avg_turns": 0.0,
    }
    if ai_campaign_ids:
        ai_runtime_totals = {
            "numbers": db.query(func.count(CampaignNumber.id)).filter(
                CampaignNumber.campaign_id.in_(ai_campaign_ids)
            ).scalar() or 0,
            "handoffs": db.query(func.count(CampaignNumber.id)).filter(
                CampaignNumber.campaign_id.in_(ai_campaign_ids),
                CampaignNumber.ai_handoff_reason.isnot(None),
            ).scalar() or 0,
            "errors": db.query(func.count(CampaignNumber.id)).filter(
                CampaignNumber.campaign_id.in_(ai_campaign_ids),
                CampaignNumber.ai_runtime_error.isnot(None),
            ).scalar() or 0,
            "silent_turns": db.query(func.coalesce(func.sum(CampaignNumber.ai_no_input_turns), 0)).filter(
                CampaignNumber.campaign_id.in_(ai_campaign_ids)
            ).scalar() or 0,
            "avg_turns": float(
                db.query(func.coalesce(func.avg(CampaignNumber.ai_turn_count), 0)).filter(
                    CampaignNumber.campaign_id.in_(ai_campaign_ids),
                    CampaignNumber.ai_turn_count.isnot(None),
                ).scalar() or 0.0
            ),
        }
    ai_agents_total = db.query(func.count(AIAgent.id)).filter(AIAgent.user_id == user.id).scalar() or 0
    active_ai_agents_total = db.query(func.count(AIAgent.id)).filter(
        AIAgent.user_id == user.id,
        AIAgent.is_active == True,
    ).scalar() or 0
    recent_ai_campaigns = [c for c in campaigns if c.campaign_mode == CampaignMode.AI_AGENT][:3]

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
        "openai_configured": has_user_openai_credentials(db, user.id),
        "elevenlabs_configured": has_user_elevenlabs_credentials(db, user.id),
        "ai_runtime_configured": has_user_elevenlabs_agent_runtime_credentials(db, user.id),
        "legacy_ai_runtime_configured": has_user_ai_runtime_credentials(db, user.id),
        "twilio_balance": twilio_balance,
        "twilio_balance_currency": twilio_balance_currency,
        "twilio_balance_error": twilio_balance_error,
        "ai_campaigns": len(ai_campaigns),
        "ai_agents_total": ai_agents_total,
        "ai_agents_active": active_ai_agents_total,
        "ai_runtime_numbers": ai_runtime_totals["numbers"],
        "ai_runtime_handoffs": ai_runtime_totals["handoffs"],
        "ai_runtime_errors": ai_runtime_totals["errors"],
        "ai_runtime_silent_turns": ai_runtime_totals["silent_turns"],
        "ai_runtime_avg_turns": ai_runtime_totals["avg_turns"],
    }
    active_rental = get_active_rental(db, user.id)
    stats["rental_active"] = bool(active_rental)

    return templates.TemplateResponse(
        "dashboard/index.html",
        {
            "request": request,
            "user": user,
            "campaigns": campaigns,
            "recent_ai_campaigns": recent_ai_campaigns,
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
    signalwire_saved: bool = False,
    telnyx_saved: bool = False,
    vonage_saved: bool = False,
    voximplant_saved: bool = False,
    openai_saved: bool = False,
    elevenlabs_saved: bool = False,
    elevenlabs_checked: bool = False,
    elevenlabs_check_error: str | None = None,
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
            signalwire_saved=signalwire_saved,
            telnyx_saved=telnyx_saved,
            vonage_saved=vonage_saved,
            voximplant_saved=voximplant_saved,
            openai_saved=openai_saved,
            elevenlabs_saved=elevenlabs_saved,
            elevenlabs_checked=elevenlabs_checked,
            elevenlabs_check_error=elevenlabs_check_error,
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


@router.post("/settings/signalwire")
async def save_signalwire_credentials(
    request: Request,
    project_id: str = Form(...),
    api_token: str = Form(...),
    space_url: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Save user's SignalWire credentials."""
    project_id = project_id.strip()
    api_token = api_token.strip()
    space_url = space_url.strip()

    if not project_id:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="SignalWire Project ID cannot be empty."),
            status_code=400
        )

    if not api_token:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="SignalWire API Token cannot be empty."),
            status_code=400
        )

    if not space_url:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="SignalWire Space URL cannot be empty."),
            status_code=400
        )

    upsert_user_signalwire_credentials(db, user.id, project_id, api_token, space_url)
    db.commit()

    return RedirectResponse(url="/dashboard/settings?signalwire_saved=true", status_code=302)


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


@router.post("/settings/openai")
async def save_openai_credentials(
    request: Request,
    api_key: str = Form(...),
    organization_id: str = Form(default=""),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    api_key = api_key.strip()
    organization_id = organization_id.strip()

    if not api_key.startswith("sk-"):
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="OpenAI API key must start with sk-."),
            status_code=400,
        )

    upsert_user_openai_credentials(db, user.id, api_key, organization_id)
    db.commit()
    return RedirectResponse(url="/dashboard/settings?openai_saved=true", status_code=302)


@router.post("/settings/elevenlabs")
async def save_elevenlabs_credentials(
    request: Request,
    api_key: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    api_key = api_key.strip()

    if len(api_key) < 20:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(request, user, db, error="ElevenLabs API key looks invalid."),
            status_code=400,
        )

    upsert_user_elevenlabs_credentials(db, user.id, api_key)
    db.commit()
    return RedirectResponse(url="/dashboard/settings?elevenlabs_saved=true", status_code=302)


@router.post("/settings/elevenlabs/check")
async def check_elevenlabs_credentials_route(
    request: Request,
    api_key: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db),
):
    api_key = api_key.strip()
    if len(api_key) < 20:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(
                request,
                user,
                db,
                error="ElevenLabs API key looks invalid.",
            ),
            status_code=400,
        )

    ok, message = check_elevenlabs_credentials(api_key)
    if not ok:
        return templates.TemplateResponse(
            "dashboard/settings.html",
            _settings_context(
                request,
                user,
                db,
                elevenlabs_checked=True,
                elevenlabs_check_error=message,
            ),
            status_code=400,
        )
    return templates.TemplateResponse(
        "dashboard/settings.html",
        _settings_context(
            request,
            user,
            db,
            elevenlabs_checked=True,
        ),
        status_code=200,
    )
