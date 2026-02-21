"""
FastAPI Dependencies
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import decode_access_token
from app.database import get_db
from app.models import User
from app.services.rental_service import has_active_rental
from app.services.user_voice_provider_service import has_any_user_voice_provider_credentials


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user from JWT cookie"""
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}
        )

    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload"
        )

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    return user


async def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user if authenticated, otherwise None"""
    try:
        return await get_current_user(request, db)
    except HTTPException:
        return None


async def get_admin_user(
    user: User = Depends(get_current_user)
) -> User:
    """Require admin privileges"""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return user


async def require_transfer_configured(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> User:
    """Require user to have transfer number and at least one voice provider configured."""
    if not user.transfer_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please configure your Transfer Number (3CX) in Settings first"
        )
    if not has_any_user_voice_provider_credentials(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please configure at least one voice provider (Twilio, Telnyx, or Vonage) in Settings first"
        )
    return user


async def require_active_rental(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> User:
    """Require an active rental period for non-admin users."""
    if user.is_admin:
        return user

    if not has_active_rental(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Active rental required. Please renew your daily/weekly plan."
        )

    return user


# Alias for backwards compatibility
require_twilio_configured = require_transfer_configured
