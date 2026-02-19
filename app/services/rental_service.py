"""
Rental service - plans and active rental enforcement helpers
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import RentalPlan, UserRental, RentalStatus


DEFAULT_RENTAL_PLANS = (
    {"code": "daily", "name": "Daily Rental", "duration_days": 1, "price_usdt": 15.0},
    {"code": "weekly", "name": "Weekly Rental", "duration_days": 7, "price_usdt": 75.0},
)


def ensure_default_rental_plans(db: Session) -> None:
    """Create default rental plans if they don't exist."""
    for plan_data in DEFAULT_RENTAL_PLANS:
        existing = db.query(RentalPlan).filter(RentalPlan.code == plan_data["code"]).first()
        if existing:
            continue

        db.add(RentalPlan(**plan_data, is_active=True))

    db.commit()


def get_active_plan(db: Session, plan_id: int) -> Optional[RentalPlan]:
    return db.query(RentalPlan).filter(
        RentalPlan.id == plan_id,
        RentalPlan.is_active == True
    ).first()


def get_active_rental(db: Session, user_id: int) -> Optional[UserRental]:
    now = datetime.utcnow()

    # Keep statuses up-to-date lazily.
    expired = db.query(UserRental).filter(
        UserRental.user_id == user_id,
        UserRental.status == RentalStatus.ACTIVE,
        UserRental.expires_at <= now
    ).all()
    if expired:
        for item in expired:
            item.status = RentalStatus.EXPIRED
        db.commit()

    return db.query(UserRental).filter(
        UserRental.user_id == user_id,
        UserRental.status == RentalStatus.ACTIVE,
        UserRental.expires_at > now
    ).order_by(UserRental.expires_at.desc()).first()


def has_active_rental(db: Session, user_id: int) -> bool:
    return get_active_rental(db, user_id) is not None


def activate_or_extend_rental(db: Session, user_id: int, plan: RentalPlan) -> UserRental:
    """Create a new rental period, extending from current expiry when active."""
    now = datetime.utcnow()
    current = get_active_rental(db, user_id)

    starts_at = now
    if current and current.expires_at > now:
        starts_at = current.expires_at

    expires_at = starts_at + timedelta(days=plan.duration_days)

    rental = UserRental(
        user_id=user_id,
        plan_id=plan.id,
        starts_at=starts_at,
        expires_at=expires_at,
        status=RentalStatus.ACTIVE,
    )
    db.add(rental)
    db.flush()

    return rental
