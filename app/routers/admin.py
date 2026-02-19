"""
Admin Router - Management of CallerIDs, Countries, Audios
"""
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import User, CallerID, Country, Audio, Campaign, CampaignStatus, Payment, PaymentStatus
from app.services.r2_service import r2_service
from app.services.system_settings_service import (
    TWILIO_ACCOUNT_SID_KEY,
    TWILIO_AUTH_TOKEN_KEY,
    get_twilio_credentials,
    upsert_setting,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    twilio_saved: bool = False,
    error: Optional[str] = None,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Admin dashboard"""
    twilio_sid, twilio_token = get_twilio_credentials(db)

    stats = {
        "total_users": db.query(User).filter(User.is_admin == False).count(),
        "total_campaigns": db.query(Campaign).count(),
        "active_caller_ids": db.query(CallerID).filter(CallerID.is_active == True).count(),
        "total_audios": db.query(Audio).filter(Audio.is_active == True).count(),
        "total_countries": db.query(Country).filter(Country.is_active == True).count(),
        "pending_payments": db.query(Payment).filter(Payment.status == PaymentStatus.PENDING).count()
    }

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "twilio_saved": twilio_saved,
            "error": error,
            "twilio_account_sid": twilio_sid,
            "twilio_configured": bool(twilio_sid and twilio_token),
        }
    )


@router.post("/settings/twilio")
async def save_twilio_settings(
    account_sid: str = Form(...),
    auth_token: str = Form(...),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Update global Twilio credentials from admin dashboard."""
    del user  # dependency ensures admin permission

    account_sid = account_sid.strip()
    auth_token = auth_token.strip()

    if not account_sid.startswith("AC") or len(account_sid) != 34:
        msg = quote("Invalid Account SID format")
        return RedirectResponse(url=f"/admin?error={msg}", status_code=302)

    if not auth_token:
        msg = quote("Auth Token cannot be empty")
        return RedirectResponse(url=f"/admin?error={msg}", status_code=302)

    upsert_setting(db, TWILIO_ACCOUNT_SID_KEY, account_sid)
    upsert_setting(db, TWILIO_AUTH_TOKEN_KEY, auth_token)
    db.commit()

    return RedirectResponse(url="/admin?twilio_saved=true", status_code=302)


# ============== CallerID CRUD ==============

@router.get("/caller-ids", response_class=HTMLResponse)
async def list_caller_ids(
    request: Request,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """List all caller IDs"""
    caller_ids = db.query(CallerID).order_by(CallerID.country_code, CallerID.phone_number).all()

    return templates.TemplateResponse(
        "admin/caller_ids/list.html",
        {
            "request": request,
            "user": user,
            "caller_ids": caller_ids
        }
    )


@router.get("/caller-ids/create", response_class=HTMLResponse)
async def create_caller_id_page(
    request: Request,
    user: User = Depends(get_admin_user)
):
    """Create caller ID form"""
    return templates.TemplateResponse(
        "admin/caller_ids/create.html",
        {
            "request": request,
            "user": user,
            "error": None
        }
    )


@router.post("/caller-ids/create")
async def create_caller_id(
    phone_number: str = Form(...),
    country_code: str = Form(...),
    description: str = Form(default=""),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Create a new caller ID"""
    # Check if exists
    existing = db.query(CallerID).filter(CallerID.phone_number == phone_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="Phone number already exists")

    caller_id = CallerID(
        phone_number=phone_number,
        country_code=country_code.upper(),
        description=description
    )
    db.add(caller_id)
    db.commit()

    return RedirectResponse(url="/admin/caller-ids", status_code=302)


@router.get("/caller-ids/{caller_id_id}/edit", response_class=HTMLResponse)
async def edit_caller_id_page(
    request: Request,
    caller_id_id: int,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Edit caller ID form"""
    caller_id = db.query(CallerID).filter(CallerID.id == caller_id_id).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    return templates.TemplateResponse(
        "admin/caller_ids/edit.html",
        {
            "request": request,
            "user": user,
            "caller_id": caller_id,
            "error": None
        }
    )


@router.post("/caller-ids/{caller_id_id}/edit")
async def edit_caller_id(
    caller_id_id: int,
    phone_number: str = Form(...),
    country_code: str = Form(...),
    description: str = Form(default=""),
    is_active: bool = Form(default=False),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Update a caller ID"""
    caller_id = db.query(CallerID).filter(CallerID.id == caller_id_id).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    caller_id.phone_number = phone_number
    caller_id.country_code = country_code.upper()
    caller_id.description = description
    caller_id.is_active = is_active
    db.commit()

    return RedirectResponse(url="/admin/caller-ids", status_code=302)


@router.post("/caller-ids/{caller_id_id}/delete")
async def delete_caller_id(
    caller_id_id: int,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Delete a caller ID"""
    caller_id = db.query(CallerID).filter(CallerID.id == caller_id_id).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    db.delete(caller_id)
    db.commit()

    return RedirectResponse(url="/admin/caller-ids", status_code=302)


# ============== Country CRUD ==============

@router.get("/countries", response_class=HTMLResponse)
async def list_countries(
    request: Request,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """List all countries"""
    countries = db.query(Country).order_by(Country.code).all()

    return templates.TemplateResponse(
        "admin/countries/list.html",
        {
            "request": request,
            "user": user,
            "countries": countries
        }
    )


@router.get("/countries/create", response_class=HTMLResponse)
async def create_country_page(
    request: Request,
    user: User = Depends(get_admin_user)
):
    """Create country form"""
    return templates.TemplateResponse(
        "admin/countries/create.html",
        {
            "request": request,
            "user": user,
            "error": None
        }
    )


@router.post("/countries/create")
async def create_country(
    code: str = Form(...),
    name: str = Form(...),
    price_per_minute: float = Form(...),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Create a new country"""
    existing = db.query(Country).filter(Country.code == code.upper()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Country code already exists")

    country = Country(
        code=code.upper(),
        name=name,
        price_per_minute=price_per_minute
    )
    db.add(country)
    db.commit()

    return RedirectResponse(url="/admin/countries", status_code=302)


@router.get("/countries/{country_id}/edit", response_class=HTMLResponse)
async def edit_country_page(
    request: Request,
    country_id: int,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Edit country form"""
    country = db.query(Country).filter(Country.id == country_id).first()
    if not country:
        raise HTTPException(status_code=404, detail="Country not found")

    return templates.TemplateResponse(
        "admin/countries/edit.html",
        {
            "request": request,
            "user": user,
            "country": country,
            "error": None
        }
    )


@router.post("/countries/{country_id}/edit")
async def edit_country(
    country_id: int,
    code: str = Form(...),
    name: str = Form(...),
    price_per_minute: float = Form(...),
    is_active: bool = Form(default=False),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Update a country"""
    country = db.query(Country).filter(Country.id == country_id).first()
    if not country:
        raise HTTPException(status_code=404, detail="Country not found")

    country.code = code.upper()
    country.name = name
    country.price_per_minute = price_per_minute
    country.is_active = is_active
    db.commit()

    return RedirectResponse(url="/admin/countries", status_code=302)


# ============== Audio CRUD ==============

@router.get("/audios", response_class=HTMLResponse)
async def list_audios(
    request: Request,
    error: Optional[str] = None,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """List all audios"""
    audios = db.query(Audio).order_by(Audio.created_at.desc()).all()

    return templates.TemplateResponse(
        "admin/audios/list.html",
        {
            "request": request,
            "user": user,
            "audios": audios,
            "error": error
        }
    )


@router.get("/audios/upload", response_class=HTMLResponse)
async def upload_audio_page(
    request: Request,
    user: User = Depends(get_admin_user)
):
    """Upload audio form"""
    return templates.TemplateResponse(
        "admin/audios/upload.html",
        {
            "request": request,
            "user": user,
            "error": None
        }
    )


@router.post("/audios/upload")
async def upload_audio(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Upload a new audio file"""
    # Validate file type
    allowed_types = ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/ogg']
    if file.content_type not in allowed_types:
        return templates.TemplateResponse(
            "admin/audios/upload.html",
            {
                "request": request,
                "user": user,
                "error": f"Invalid file type. Allowed: MP3, WAV, OGG"
            },
            status_code=400
        )

    # Upload to R2
    content = await file.read()
    result = r2_service.upload_audio(content, file.filename, file.content_type)

    audio = Audio(
        name=name,
        r2_key=result['key'],
        r2_url=result['url']
    )
    db.add(audio)
    db.commit()

    return RedirectResponse(url="/admin/audios", status_code=302)


@router.get("/audios/{audio_id}/edit", response_class=HTMLResponse)
async def edit_audio_page(
    request: Request,
    audio_id: int,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Edit audio form"""
    audio = db.query(Audio).filter(Audio.id == audio_id).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    return templates.TemplateResponse(
        "admin/audios/edit.html",
        {
            "request": request,
            "user": user,
            "audio": audio,
            "error": None
        }
    )


@router.post("/audios/{audio_id}/edit")
async def edit_audio(
    audio_id: int,
    name: str = Form(...),
    is_active: bool = Form(default=False),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Update an audio"""
    audio = db.query(Audio).filter(Audio.id == audio_id).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    audio.name = name
    audio.is_active = is_active
    db.commit()

    return RedirectResponse(url="/admin/audios", status_code=302)


@router.post("/audios/{audio_id}/delete")
async def delete_audio(
    audio_id: int,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Delete an audio"""
    audio = db.query(Audio).filter(Audio.id == audio_id).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    if audio.r2_key == "system/deleted-audio":
        msg = quote("Cannot delete system archived audio")
        return RedirectResponse(url=f"/admin/audios?error={msg}", status_code=302)

    active_campaigns_count = db.query(Campaign).filter(
        Campaign.audio_id == audio.id,
        Campaign.status.in_([CampaignStatus.DRAFT, CampaignStatus.RUNNING, CampaignStatus.PAUSED])
    ).count()
    if active_campaigns_count > 0:
        msg = quote(f"Cannot delete audio: it is used by {active_campaigns_count} active campaign(s)")
        return RedirectResponse(url=f"/admin/audios?error={msg}", status_code=302)

    finalized_campaigns = db.query(Campaign).filter(
        Campaign.audio_id == audio.id,
        Campaign.status.in_([CampaignStatus.COMPLETED, CampaignStatus.CANCELLED])
    ).all()

    if finalized_campaigns:
        archived_audio = db.query(Audio).filter(Audio.r2_key == "system/deleted-audio").first()
        if not archived_audio:
            archived_audio = Audio(
                name="[Archived] Deleted audio",
                r2_key="system/deleted-audio",
                r2_url="#",
                is_active=False
            )
            db.add(archived_audio)
            db.flush()

        for campaign in finalized_campaigns:
            campaign.audio_id = archived_audio.id

    # Delete from R2
    r2_service.delete_audio(audio.r2_key)

    try:
        db.delete(audio)
        db.commit()
    except IntegrityError:
        db.rollback()
        msg = quote("Cannot delete audio because it is linked to existing records")
        return RedirectResponse(url=f"/admin/audios?error={msg}", status_code=302)

    return RedirectResponse(url="/admin/audios", status_code=302)


# ============== Users Management ==============

@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    created: bool = False,
    deleted: bool = False,
    error: Optional[str] = None,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """List all users"""
    users = db.query(User).order_by(User.created_at.desc()).all()

    return templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "created": created,
            "deleted": deleted,
            "error": error
        }
    )


@router.post("/users/create")
async def create_user(
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Create a new non-admin user from admin panel"""
    del admin  # dependency ensures admin permission

    email = email.lower().strip()

    # Enforce max user limit for non-admin accounts
    user_count = db.query(User).filter(User.is_admin == False).count()
    if user_count >= settings.MAX_USERS:
        msg = quote("Maximum users reached")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    if password != password_confirm:
        msg = quote("Passwords do not match")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    if len(password) < 6:
        msg = quote("Password must be at least 6 characters")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        msg = quote("Email already registered")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    user = User(
        email=email,
        password_hash=hash_password(password),
        is_admin=False,
        is_active=True
    )
    db.add(user)
    db.commit()

    return RedirectResponse(url="/admin/users?created=true", status_code=302)


@router.post("/users/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Toggle user active status"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")

    user.is_active = not user.is_active
    db.commit()

    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/add-credits")
async def add_credits(
    user_id: int,
    amount: float = Form(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Manually add credits to a user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.credits += amount
    db.commit()

    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Delete a non-admin user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        msg = quote("Cannot delete yourself")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    if user.is_admin:
        msg = quote("Cannot delete admin users")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    db.delete(user)
    db.commit()

    return RedirectResponse(url="/admin/users?deleted=true", status_code=302)
