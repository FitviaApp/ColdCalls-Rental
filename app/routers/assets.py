"""
Assets Router - user-managed Caller IDs and Audios
"""
from datetime import datetime
import re
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_active_rental
from app.models import User, CallerID, Audio, Campaign, CampaignStatus, VoxCallerIDVerificationStatus
from app.templating import Jinja2Templates
from app.services.r2_service import r2_service
from app.services.user_voximplant_service import get_user_voximplant_credentials
from app.services.voximplant_management_service import (
    VoximplantManagementService,
    ensure_voximplant_caller_id,
)

router = APIRouter(prefix="/assets", tags=["assets"])
templates = Jinja2Templates(directory="app/templates")

E164_PATTERN = re.compile(r'^\+[1-9]\d{1,14}$')


@router.get("/caller-ids", response_class=HTMLResponse)
async def list_caller_ids(
    request: Request,
    created: bool = False,
    deleted: bool = False,
    error: Optional[str] = None,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """List current user's caller IDs."""
    caller_ids = db.query(CallerID).filter(
        CallerID.user_id == user.id
    ).order_by(CallerID.country_code, CallerID.phone_number).all()

    return templates.TemplateResponse(
        "assets/caller_ids/list.html",
        {
            "request": request,
            "user": user,
            "caller_ids": caller_ids,
            "voximplant_configured": bool(get_user_voximplant_credentials(db, user.id)),
            "created": created,
            "deleted": deleted,
            "verified": request.query_params.get("verified") == "true",
            "error": error,
        }
    )


@router.get("/caller-ids/create", response_class=HTMLResponse)
async def create_caller_id_page(
    request: Request,
    user: User = Depends(require_active_rental)
):
    return templates.TemplateResponse(
        "assets/caller_ids/create.html",
        {
            "request": request,
            "user": user,
            "error": None,
            "form_data": {},
        }
    )


@router.post("/caller-ids/create")
async def create_caller_id(
    request: Request,
    phone_number: str = Form(...),
    country_code: str = Form(...),
    description: str = Form(default=""),
    elevenlabs_phone_number_id: str = Form(default=""),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    phone_number = phone_number.strip()
    country_code = country_code.strip().upper()
    elevenlabs_phone_number_id = elevenlabs_phone_number_id.strip()

    if not E164_PATTERN.match(phone_number):
        return templates.TemplateResponse(
            "assets/caller_ids/create.html",
            {
                "request": request,
                "user": user,
                "error": "Invalid phone number. Use E.164 format (e.g., +15551234567).",
                "form_data": {
                    "phone_number": phone_number,
                    "country_code": country_code,
                    "description": description,
                    "elevenlabs_phone_number_id": elevenlabs_phone_number_id,
                },
            },
            status_code=400,
        )

    existing = db.query(CallerID).filter(CallerID.phone_number == phone_number).first()
    if existing:
        return templates.TemplateResponse(
            "assets/caller_ids/create.html",
            {
                "request": request,
                "user": user,
                "error": "This phone number is already registered.",
                "form_data": {
                    "phone_number": phone_number,
                    "country_code": country_code,
                    "description": description,
                    "elevenlabs_phone_number_id": elevenlabs_phone_number_id,
                },
            },
            status_code=400,
        )

    caller_id = CallerID(
        user_id=user.id,
        phone_number=phone_number,
        country_code=country_code,
        description=description.strip(),
        elevenlabs_phone_number_id=elevenlabs_phone_number_id or None,
        is_active=True,
        vox_verification_status=VoxCallerIDVerificationStatus.NOT_STARTED,
    )
    db.add(caller_id)
    db.commit()

    return RedirectResponse(url="/assets/caller-ids?created=true", status_code=302)


@router.get("/caller-ids/{caller_id_id}/edit", response_class=HTMLResponse)
async def edit_caller_id_page(
    request: Request,
    caller_id_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.user_id == user.id
    ).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    return templates.TemplateResponse(
        "assets/caller_ids/edit.html",
        {
            "request": request,
            "user": user,
            "caller_id": caller_id,
            "error": None,
            "form_data": {},
        }
    )


@router.post("/caller-ids/{caller_id_id}/edit")
async def edit_caller_id(
    request: Request,
    caller_id_id: int,
    phone_number: str = Form(...),
    country_code: str = Form(...),
    description: str = Form(default=""),
    elevenlabs_phone_number_id: str = Form(default=""),
    is_active: bool = Form(default=False),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.user_id == user.id
    ).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    phone_number = phone_number.strip()
    country_code = country_code.strip().upper()
    elevenlabs_phone_number_id = elevenlabs_phone_number_id.strip()

    if not E164_PATTERN.match(phone_number):
        return templates.TemplateResponse(
            "assets/caller_ids/edit.html",
            {
                "request": request,
                "user": user,
                "caller_id": caller_id,
                "error": "Invalid phone number. Use E.164 format (e.g., +15551234567).",
                "form_data": {
                    "phone_number": phone_number,
                    "country_code": country_code,
                    "description": description,
                    "is_active": is_active,
                    "elevenlabs_phone_number_id": elevenlabs_phone_number_id,
                },
            },
            status_code=400,
        )

    existing = db.query(CallerID).filter(
        CallerID.phone_number == phone_number,
        CallerID.id != caller_id.id
    ).first()
    if existing:
        return templates.TemplateResponse(
            "assets/caller_ids/edit.html",
            {
                "request": request,
                "user": user,
                "caller_id": caller_id,
                "error": "This phone number is already registered.",
                "form_data": {
                    "phone_number": phone_number,
                    "country_code": country_code,
                    "description": description,
                    "is_active": is_active,
                    "elevenlabs_phone_number_id": elevenlabs_phone_number_id,
                },
            },
            status_code=400,
        )

    phone_number_changed = caller_id.phone_number != phone_number
    caller_id.phone_number = phone_number
    caller_id.country_code = country_code
    caller_id.description = description.strip()
    caller_id.elevenlabs_phone_number_id = elevenlabs_phone_number_id or None
    caller_id.is_active = is_active
    if phone_number_changed:
        caller_id.vox_callerid_id = None
        caller_id.vox_verification_status = VoxCallerIDVerificationStatus.NOT_STARTED
        caller_id.vox_last_verification_at = None
    db.commit()

    return RedirectResponse(url="/assets/caller-ids", status_code=302)


@router.post("/caller-ids/{caller_id_id}/voximplant/verify")
async def start_voximplant_verification(
    caller_id_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.user_id == user.id
    ).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    credentials = get_user_voximplant_credentials(db, user.id)
    if not credentials:
        msg = quote("Configure Voximplant credentials in Settings before verifying numbers.")
        return RedirectResponse(url=f"/assets/caller-ids?error={msg}", status_code=302)

    try:
        response = ensure_voximplant_caller_id(caller_id, credentials)
        caller_id.vox_callerid_id = int(
            response.get("callerid_id")
            or response.get("caller_id")
            or caller_id.vox_callerid_id
            or 0
        ) or None
        VoximplantManagementService(credentials).verify_caller_id(caller_id.vox_callerid_id)
        caller_id.vox_verification_status = VoxCallerIDVerificationStatus.PENDING
        caller_id.vox_last_verification_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        caller_id.vox_verification_status = VoxCallerIDVerificationStatus.FAILED
        db.commit()
        msg = quote(f"Voximplant verification failed: {exc}")
        return RedirectResponse(url=f"/assets/caller-ids?error={msg}", status_code=302)

    return RedirectResponse(url="/assets/caller-ids", status_code=302)


@router.post("/caller-ids/{caller_id_id}/voximplant/activate")
async def activate_voximplant_caller_id(
    caller_id_id: int,
    verification_code: str = Form(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.user_id == user.id
    ).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    credentials = get_user_voximplant_credentials(db, user.id)
    if not credentials or not caller_id.vox_callerid_id:
        msg = quote("Start Voximplant verification before activating this number.")
        return RedirectResponse(url=f"/assets/caller-ids?error={msg}", status_code=302)

    try:
        VoximplantManagementService(credentials).activate_caller_id(
            caller_id.vox_callerid_id,
            verification_code.strip(),
        )
        caller_id.vox_verification_status = VoxCallerIDVerificationStatus.VERIFIED
        db.commit()
    except Exception as exc:
        caller_id.vox_verification_status = VoxCallerIDVerificationStatus.FAILED
        db.commit()
        msg = quote(f"Activation failed: {exc}")
        return RedirectResponse(url=f"/assets/caller-ids?error={msg}", status_code=302)

    return RedirectResponse(url="/assets/caller-ids?verified=true", status_code=302)


@router.post("/caller-ids/{caller_id_id}/delete")
async def delete_caller_id(
    caller_id_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    caller_id = db.query(CallerID).filter(
        CallerID.id == caller_id_id,
        CallerID.user_id == user.id
    ).first()
    if not caller_id:
        raise HTTPException(status_code=404, detail="Caller ID not found")

    campaigns_count = db.query(Campaign).filter(Campaign.caller_id_id == caller_id.id).count()
    if campaigns_count > 0:
        msg = quote("Cannot delete: this Caller ID is already used by campaigns")
        return RedirectResponse(url=f"/assets/caller-ids?error={msg}", status_code=302)

    db.delete(caller_id)
    db.commit()

    return RedirectResponse(url="/assets/caller-ids?deleted=true", status_code=302)


@router.get("/audios", response_class=HTMLResponse)
async def list_audios(
    request: Request,
    uploaded: bool = False,
    deleted: bool = False,
    error: Optional[str] = None,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    audios = db.query(Audio).filter(
        Audio.user_id == user.id
    ).order_by(Audio.created_at.desc()).all()

    return templates.TemplateResponse(
        "assets/audios/list.html",
        {
            "request": request,
            "user": user,
            "audios": audios,
            "uploaded": uploaded,
            "deleted": deleted,
            "error": error,
        }
    )


@router.get("/audios/upload", response_class=HTMLResponse)
async def upload_audio_page(
    request: Request,
    user: User = Depends(require_active_rental)
):
    return templates.TemplateResponse(
        "assets/audios/upload.html",
        {
            "request": request,
            "user": user,
            "error": None,
        }
    )


@router.post("/audios/upload")
async def upload_audio(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    allowed_types = ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/ogg']
    if file.content_type not in allowed_types:
        return templates.TemplateResponse(
            "assets/audios/upload.html",
            {
                "request": request,
                "user": user,
                "error": "Invalid file type. Allowed: MP3, WAV, OGG",
            },
            status_code=400,
        )

    content = await file.read()
    result = r2_service.upload_audio(content, file.filename, file.content_type)

    audio = Audio(
        user_id=user.id,
        name=name.strip(),
        r2_key=result['key'],
        r2_url=result['url'],
        is_active=True,
    )
    db.add(audio)
    db.commit()

    return RedirectResponse(url="/assets/audios?uploaded=true", status_code=302)


@router.get("/audios/{audio_id}/edit", response_class=HTMLResponse)
async def edit_audio_page(
    request: Request,
    audio_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    audio = db.query(Audio).filter(
        Audio.id == audio_id,
        Audio.user_id == user.id
    ).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    return templates.TemplateResponse(
        "assets/audios/edit.html",
        {
            "request": request,
            "user": user,
            "audio": audio,
            "error": None,
        }
    )


@router.post("/audios/{audio_id}/edit")
async def edit_audio(
    audio_id: int,
    name: str = Form(...),
    is_active: bool = Form(default=False),
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    audio = db.query(Audio).filter(
        Audio.id == audio_id,
        Audio.user_id == user.id
    ).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    audio.name = name.strip()
    audio.is_active = is_active
    db.commit()

    return RedirectResponse(url="/assets/audios", status_code=302)


@router.post("/audios/{audio_id}/delete")
async def delete_audio(
    audio_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    audio = db.query(Audio).filter(
        Audio.id == audio_id,
        Audio.user_id == user.id
    ).first()
    if not audio:
        raise HTTPException(status_code=404, detail="Audio not found")

    active_campaigns_count = db.query(Campaign).filter(
        Campaign.audio_id == audio.id,
        Campaign.status.in_([CampaignStatus.DRAFT, CampaignStatus.RUNNING, CampaignStatus.PAUSED])
    ).count()
    if active_campaigns_count > 0:
        msg = quote("Cannot delete: this audio is used by active campaigns")
        return RedirectResponse(url=f"/assets/audios?error={msg}", status_code=302)

    finalized_campaigns_count = db.query(Campaign).filter(
        Campaign.audio_id == audio.id,
        Campaign.status.in_([CampaignStatus.COMPLETED, CampaignStatus.CANCELLED])
    ).count()
    if finalized_campaigns_count > 0:
        msg = quote("Cannot delete: this audio is used by historical campaigns")
        return RedirectResponse(url=f"/assets/audios?error={msg}", status_code=302)

    r2_service.delete_audio(audio.r2_key)
    db.delete(audio)
    db.commit()

    return RedirectResponse(url="/assets/audios?deleted=true", status_code=302)
