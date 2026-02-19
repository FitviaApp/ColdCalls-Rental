"""
Authentication Router - Login, Register, Logout
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import create_access_token, verify_password
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    registration: str = "",
    user: User = Depends(get_current_user_optional)
):
    """Display login page"""
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)

    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
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
    _request: Request,
    user: User = Depends(get_current_user_optional)
):
    """Public registration is disabled; admin creates users"""
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
