"""
User-specific Twilio credentials service
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserTwilioCredential


def get_user_twilio_credentials(db: Session, user_id: int) -> tuple[str, str]:
    """Return decrypted Twilio credentials for a user."""
    row = db.query(UserTwilioCredential).filter(UserTwilioCredential.user_id == user_id).first()
    if not row:
        return "", ""

    try:
        account_sid = decrypt_twilio_credentials(row.account_sid_encrypted)
        auth_token = decrypt_twilio_credentials(row.auth_token_encrypted)
    except Exception:
        return "", ""

    return account_sid, auth_token


def has_user_twilio_credentials(db: Session, user_id: int) -> bool:
    """Check if user has valid Twilio credentials saved."""
    account_sid, auth_token = get_user_twilio_credentials(db, user_id)
    return bool(account_sid and auth_token)


def upsert_user_twilio_credentials(
    db: Session,
    user_id: int,
    account_sid: str,
    auth_token: str
) -> UserTwilioCredential:
    """Create or update encrypted Twilio credentials for a user."""
    row = db.query(UserTwilioCredential).filter(UserTwilioCredential.user_id == user_id).first()

    encrypted_sid = encrypt_twilio_credentials(account_sid.strip())
    encrypted_token = encrypt_twilio_credentials(auth_token.strip())

    if row:
        row.account_sid_encrypted = encrypted_sid
        row.auth_token_encrypted = encrypted_token
    else:
        row = UserTwilioCredential(
            user_id=user_id,
            account_sid_encrypted=encrypted_sid,
            auth_token_encrypted=encrypted_token
        )
        db.add(row)

    db.flush()
    return row
