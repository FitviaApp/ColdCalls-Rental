"""
User-specific Telnyx credentials service
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserTelnyxCredential


def get_user_telnyx_credentials(db: Session, user_id: int) -> tuple[str, str]:
    """Return decrypted Telnyx credentials (api_key, account_sid) for a user."""
    row = db.query(UserTelnyxCredential).filter(UserTelnyxCredential.user_id == user_id).first()
    if not row:
        return "", ""

    try:
        api_key = decrypt_twilio_credentials(row.api_key_encrypted)
        account_sid = decrypt_twilio_credentials(row.account_sid_encrypted)
    except Exception:
        return "", ""

    return api_key, account_sid


def has_user_telnyx_credentials(db: Session, user_id: int) -> bool:
    """Check if user has valid Telnyx credentials saved."""
    api_key, account_sid = get_user_telnyx_credentials(db, user_id)
    return bool(api_key and account_sid)


def upsert_user_telnyx_credentials(
    db: Session,
    user_id: int,
    api_key: str,
    account_sid: str
) -> UserTelnyxCredential:
    """Create or update encrypted Telnyx credentials for a user."""
    row = db.query(UserTelnyxCredential).filter(UserTelnyxCredential.user_id == user_id).first()

    encrypted_api_key = encrypt_twilio_credentials(api_key.strip())
    encrypted_account_sid = encrypt_twilio_credentials(account_sid.strip())

    if row:
        row.api_key_encrypted = encrypted_api_key
        row.account_sid_encrypted = encrypted_account_sid
    else:
        row = UserTelnyxCredential(
            user_id=user_id,
            api_key_encrypted=encrypted_api_key,
            account_sid_encrypted=encrypted_account_sid
        )
        db.add(row)

    db.flush()
    return row
