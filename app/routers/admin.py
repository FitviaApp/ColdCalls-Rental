"""
Admin Router - Platform administration
"""
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import (
    User, CallerID, Country, Audio, Campaign,
    PaymentStatus, RentalPlan, RentalPayment
)
from app.services.rental_service import get_active_rental, add_paid_days

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    error: Optional[str] = None,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Admin dashboard"""
    stats = {
        "total_users": db.query(User).filter(User.is_admin == False).count(),
        "total_campaigns": db.query(Campaign).count(),
        "total_countries": db.query(Country).filter(Country.is_active == True).count(),
        "pending_rental_payments": db.query(RentalPayment).filter(RentalPayment.status == PaymentStatus.PENDING).count(),
        "active_rental_plans": db.query(RentalPlan).filter(RentalPlan.is_active == True).count(),
        "orphan_caller_ids": db.query(CallerID).filter(CallerID.user_id.is_(None)).count(),
        "orphan_audios": db.query(Audio).filter(Audio.user_id.is_(None)).count(),
    }

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "error": error,
        }
    )


@router.get("/caller-ids")
@router.get("/caller-ids/create")
@router.get("/caller-ids/{caller_id_id}/edit")
@router.post("/caller-ids/create")
@router.post("/caller-ids/{caller_id_id}/edit")
@router.post("/caller-ids/{caller_id_id}/delete")
async def deprecated_admin_caller_ids(
    caller_id_id: Optional[int] = None,
    user: User = Depends(get_admin_user)
):
    """Deprecated: Caller IDs are now managed by each user in /assets."""
    del caller_id_id, user
    msg = quote("Caller IDs are user-owned now. Ask users to manage them in Assets.")
    return RedirectResponse(url=f"/admin?error={msg}", status_code=302)


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


@router.get("/audios")
@router.get("/audios/upload")
@router.get("/audios/{audio_id}/edit")
@router.post("/audios/upload")
@router.post("/audios/{audio_id}/edit")
@router.post("/audios/{audio_id}/delete")
async def deprecated_admin_audios(
    audio_id: Optional[int] = None,
    user: User = Depends(get_admin_user)
):
    """Deprecated: Audios are now managed by each user in /assets."""
    del audio_id, user
    msg = quote("Audios are user-owned now. Ask users to manage them in Assets.")
    return RedirectResponse(url=f"/admin?error={msg}", status_code=302)


# ============== Users Management ==============

@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    created: bool = False,
    deleted: bool = False,
    notice: Optional[str] = None,
    error: Optional[str] = None,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """List all users"""
    users = db.query(User).order_by(User.created_at.desc()).all()
    rental_status = {}
    for item in users:
        rental_status[item.id] = get_active_rental(db, item.id)

    return templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "rental_status": rental_status,
            "created": created,
            "deleted": deleted,
            "notice": notice,
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


@router.post("/users/{user_id}/assign-orphan-assets")
async def assign_orphan_assets(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Assign all orphan assets (user_id is null) to a selected user."""
    del admin

    user = db.query(User).filter(User.id == user_id, User.is_admin == False).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    db.query(CallerID).filter(CallerID.user_id.is_(None)).update(
        {CallerID.user_id: user.id},
        synchronize_session=False
    )
    db.query(Audio).filter(Audio.user_id.is_(None)).update(
        {Audio.user_id: user.id},
        synchronize_session=False
    )
    db.commit()

    msg = quote(f"Assigned orphan assets to {user.email}")
    return RedirectResponse(url=f"/admin/users?notice={msg}", status_code=302)


@router.post("/users/{user_id}/add-paid-days")
async def add_paid_days_to_user(
    user_id: int,
    paid_days: int = Form(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """Manually add paid days to a user's rental from admin dashboard."""
    del admin

    if paid_days <= 0:
        msg = quote("Paid days must be greater than zero")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    user = db.query(User).filter(User.id == user_id, User.is_admin == False).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        rental = add_paid_days(db, user.id, paid_days)
    except RuntimeError:
        msg = quote("No rental plans available. Create a rental plan first.")
        return RedirectResponse(url=f"/admin/users?error={msg}", status_code=302)

    db.commit()

    msg = quote(
        f"Added {paid_days} paid day(s) to {user.email}. "
        f"Access active until {rental.expires_at.strftime('%Y-%m-%d')}."
    )
    return RedirectResponse(url=f"/admin/users?notice={msg}", status_code=302)


@router.get("/rental-plans", response_class=HTMLResponse)
async def list_rental_plans(
    request: Request,
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    plans = db.query(RentalPlan).order_by(RentalPlan.duration_days.asc()).all()
    return templates.TemplateResponse(
        "admin/rental_plans/list.html",
        {
            "request": request,
            "user": user,
            "plans": plans,
        }
    )


@router.post("/rental-plans/create")
async def create_rental_plan(
    code: str = Form(...),
    name: str = Form(...),
    duration_days: int = Form(...),
    price_usdt: float = Form(...),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    del user
    code = code.strip().lower()
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")
    if duration_days <= 0 or price_usdt <= 0:
        raise HTTPException(status_code=400, detail="Duration and price must be greater than zero")

    existing = db.query(RentalPlan).filter(RentalPlan.code == code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Plan code already exists")

    plan = RentalPlan(
        code=code,
        name=name.strip(),
        duration_days=duration_days,
        price_usdt=price_usdt,
        is_active=True,
    )
    db.add(plan)
    db.commit()

    return RedirectResponse(url="/admin/rental-plans", status_code=302)


@router.post("/rental-plans/{plan_id}/edit")
async def edit_rental_plan(
    plan_id: int,
    name: str = Form(...),
    duration_days: int = Form(...),
    price_usdt: float = Form(...),
    is_active: bool = Form(default=False),
    user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    del user
    plan = db.query(RentalPlan).filter(RentalPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if duration_days <= 0 or price_usdt <= 0:
        raise HTTPException(status_code=400, detail="Duration and price must be greater than zero")

    plan.name = name.strip()
    plan.duration_days = duration_days
    plan.price_usdt = price_usdt
    plan.is_active = is_active
    db.commit()

    return RedirectResponse(url="/admin/rental-plans", status_code=302)


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
