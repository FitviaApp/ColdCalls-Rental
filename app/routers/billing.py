"""
Billing Router - software rental plans and crypto verification
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, PaymentStatus, RentalPlan, RentalPayment
from app.templating import Jinja2Templates
from app.services.payment_service import payment_service
from app.services.rental_service import get_active_rental, get_active_plan, activate_or_extend_rental

router = APIRouter(prefix="/billing", tags=["billing"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


@router.get("", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    success: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    plans = db.query(RentalPlan).filter(RentalPlan.is_active == True).order_by(RentalPlan.duration_days.asc()).all()
    active_rental = get_active_rental(db, user.id)

    recent_payments = db.query(RentalPayment).filter(
        RentalPayment.user_id == user.id
    ).order_by(RentalPayment.created_at.desc()).limit(10).all()

    return templates.TemplateResponse(
        "billing/index.html",
        {
            "request": request,
            "user": user,
            "plans": plans,
            "active_rental": active_rental,
            "wallet_address": settings.USDT_WALLET_ADDRESS,
            "success": success,
            "payments": recent_payments,
            "error": None,
        }
    )


@router.post("/verify")
async def verify_rental_payment(
    request: Request,
    plan_id: int = Form(...),
    tx_hash: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    plan = get_active_plan(db, plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid rental plan")

    tx_hash = tx_hash.strip().lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return await _billing_error(
            request=request,
            db=db,
            user=user,
            error="Invalid transaction hash format.",
        )

    existing_rental_payment = db.query(RentalPayment).filter(RentalPayment.tx_hash == tx_hash).first()
    if existing_rental_payment:
        return await _billing_error(
            request=request,
            db=db,
            user=user,
            error="This transaction hash has already been processed.",
        )

    rental_payment = RentalPayment(
        user_id=user.id,
        plan_id=plan.id,
        tx_hash=tx_hash,
        status=PaymentStatus.PENDING,
    )
    db.add(rental_payment)
    db.commit()

    result = await payment_service.verify_usdt_transaction(tx_hash)
    if not result["valid"]:
        rental_payment.status = PaymentStatus.FAILED
        rental_payment.error_message = result["error"]
        db.commit()
        return await _billing_error(
            request=request,
            db=db,
            user=user,
            error=f"Transaction verification failed: {result['error']}",
        )

    amount = float(result["amount"])
    if amount < plan.price_usdt:
        rental_payment.status = PaymentStatus.FAILED
        rental_payment.amount_usdt = amount
        rental_payment.error_message = (
            f"Paid {amount:.2f} USDT, but selected plan requires at least {plan.price_usdt:.2f} USDT."
        )
        db.commit()
        return await _billing_error(
            request=request,
            db=db,
            user=user,
            error=rental_payment.error_message,
        )

    rental = activate_or_extend_rental(db, user.id, plan)

    rental_payment.status = PaymentStatus.CONFIRMED
    rental_payment.amount_usdt = amount
    rental_payment.verified_at = datetime.utcnow()
    rental_payment.rental_id = rental.id
    db.commit()

    return RedirectResponse(url="/billing?success=true", status_code=302)


async def _billing_error(request: Request, db: Session, user: User, error: str):
    plans = db.query(RentalPlan).filter(RentalPlan.is_active == True).order_by(RentalPlan.duration_days.asc()).all()
    active_rental = get_active_rental(db, user.id)
    recent_payments = db.query(RentalPayment).filter(
        RentalPayment.user_id == user.id
    ).order_by(RentalPayment.created_at.desc()).limit(10).all()

    return templates.TemplateResponse(
        "billing/index.html",
        {
            "request": request,
            "user": user,
            "plans": plans,
            "active_rental": active_rental,
            "wallet_address": settings.USDT_WALLET_ADDRESS,
            "success": False,
            "payments": recent_payments,
            "error": error,
        },
        status_code=400,
    )
