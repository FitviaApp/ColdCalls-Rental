"""
Payments Router - USDT deposits and verification
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, Payment, PaymentStatus
from app.services.payment_service import payment_service

router = APIRouter(prefix="/payments", tags=["payments"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


@router.get("", response_class=HTMLResponse)
async def payments_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Payments overview page"""
    payments = db.query(Payment).filter(
        Payment.user_id == user.id
    ).order_by(Payment.created_at.desc()).all()

    return templates.TemplateResponse(
        "payments/index.html",
        {
            "request": request,
            "user": user,
            "payments": payments,
            "wallet_address": settings.USDT_WALLET_ADDRESS
        }
    )


@router.get("/deposit", response_class=HTMLResponse)
async def deposit_page(
    request: Request,
    user: User = Depends(get_current_user)
):
    """Show deposit instructions"""
    return templates.TemplateResponse(
        "payments/deposit.html",
        {
            "request": request,
            "user": user,
            "wallet_address": settings.USDT_WALLET_ADDRESS,
            "rate": settings.USDT_TO_CREDITS_RATE,
            "error": None
        }
    )


@router.post("/verify")
async def verify_payment(
    request: Request,
    tx_hash: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Verify a USDT transaction"""
    # Clean tx_hash
    tx_hash = tx_hash.strip().lower()

    # Validate format
    if not tx_hash.startswith('0x') or len(tx_hash) != 66:
        return templates.TemplateResponse(
            "payments/deposit.html",
            {
                "request": request,
                "user": user,
                "wallet_address": settings.USDT_WALLET_ADDRESS,
                "rate": settings.USDT_TO_CREDITS_RATE,
                "error": "Invalid transaction hash format. Must be a 64-character hex string starting with 0x"
            },
            status_code=400
        )

    # Check if TX already processed
    existing = db.query(Payment).filter(Payment.tx_hash == tx_hash).first()
    if existing:
        return templates.TemplateResponse(
            "payments/deposit.html",
            {
                "request": request,
                "user": user,
                "wallet_address": settings.USDT_WALLET_ADDRESS,
                "rate": settings.USDT_TO_CREDITS_RATE,
                "error": "This transaction has already been processed"
            },
            status_code=400
        )

    # Create pending payment record
    payment = Payment(
        user_id=user.id,
        tx_hash=tx_hash,
        amount_usdt=0,
        credits_added=0,
        status=PaymentStatus.PENDING
    )
    db.add(payment)
    db.commit()

    # Verify with Etherscan
    result = await payment_service.verify_usdt_transaction(tx_hash)

    if result['valid']:
        # Calculate credits with markup
        credits = result['amount'] * settings.USDT_TO_CREDITS_RATE

        payment.amount_usdt = result['amount']
        payment.credits_added = credits
        payment.status = PaymentStatus.CONFIRMED
        payment.verified_at = datetime.utcnow()

        user.credits += credits
        db.commit()

        return RedirectResponse(url="/payments?success=true", status_code=302)
    else:
        payment.status = PaymentStatus.FAILED
        payment.error_message = result['error']
        db.commit()

        return templates.TemplateResponse(
            "payments/deposit.html",
            {
                "request": request,
                "user": user,
                "wallet_address": settings.USDT_WALLET_ADDRESS,
                "rate": settings.USDT_TO_CREDITS_RATE,
                "error": f"Transaction verification failed: {result['error']}"
            },
            status_code=400
        )


@router.get("/history", response_class=HTMLResponse)
async def payment_history(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Payment history page"""
    payments = db.query(Payment).filter(
        Payment.user_id == user.id
    ).order_by(Payment.created_at.desc()).all()

    return templates.TemplateResponse(
        "payments/history.html",
        {
            "request": request,
            "user": user,
            "payments": payments
        }
    )
