"""
Authentication Router - Login, Register, Logout
"""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import create_access_token, decode_access_token, verify_password
from app.config import get_settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()
logger = logging.getLogger(__name__)


def _get_authenticated_user_or_none(request: Request, db: Session) -> User | None:
    """
    Public auth pages should still render even if a stale cookie or transient DB
    issue prevents us from resolving the current user.
    """
    token = request.cookies.get("access_token")
    if not token:
        return None

    try:
        payload = decode_access_token(token)
        if not payload:
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        user = db.query(User).filter(User.id == int(user_id)).first()
        if not user or not user.is_active:
            return None

        return user
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ignoring auth cookie on public auth page: %s", exc)
        return None


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    registration: str = "",
    db: Session = Depends(get_db)
):
    """Display login page"""
    user = _get_authenticated_user_or_none(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)

    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "user": None,
            "registration_disabled": registration == "disabled",
            "error": None
        }
    )


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Process login form"""
    user = db.query(User).filter(User.email == email.lower()).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "user": None,
                "error": "Invalid email or password",
                "registration_disabled": False
            },
            status_code=400
        )

    if not user.is_active:
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "user": None,
                "error": "Your account has been disabled",
                "registration_disabled": False
            },
            status_code=403
        )

    # Create JWT token
    token = create_access_token({"sub": str(user.id)})

    # Redirect to dashboard with token in cookie
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=settings.JWT_EXPIRATION_HOURS * 3600,
        samesite="lax"
    )

    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    db: Session = Depends(get_db)
):
    """Public registration is disabled; admin creates users"""
    user = _get_authenticated_user_or_none(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)

    return RedirectResponse(url="/auth/login?registration=disabled", status_code=302)


@router.post("/register")
async def register(
    _request: Request
):
    """Public registration is disabled; admin creates users"""
    return RedirectResponse(url="/auth/login?registration=disabled", status_code=302)


@router.get("/logout")
async def logout():
    """Logout user by clearing cookie"""
    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie("access_token")
    return response
