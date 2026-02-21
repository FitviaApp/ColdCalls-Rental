"""
User-specific Vonage credentials service
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserVonageCredential


def get_user_vonage_credentials(db: Session, user_id: int) -> tuple[str, str]:
    """Return decrypted Vonage credentials (application_id, private_key_pem) for a user."""
    row = db.query(UserVonageCredential).filter(UserVonageCredential.user_id == user_id).first()
    if not row:
        return "", ""

    try:
        application_id = decrypt_twilio_credentials(row.application_id_encrypted)
        private_key = decrypt_twilio_credentials(row.private_key_encrypted)
    except Exception:
        return "", ""

    return application_id, private_key


def has_user_vonage_credentials(db: Session, user_id: int) -> bool:
    """Check if user has valid Vonage credentials saved."""
    application_id, private_key = get_user_vonage_credentials(db, user_id)
    return bool(application_id and private_key)


def upsert_user_vonage_credentials(
    db: Session,
    user_id: int,
    application_id: str,
    private_key: str
) -> UserVonageCredential:
    """Create or update encrypted Vonage credentials for a user."""
    row = db.query(UserVonageCredential).filter(UserVonageCredential.user_id == user_id).first()

    encrypted_app_id = encrypt_twilio_credentials(application_id.strip())
    encrypted_private_key = encrypt_twilio_credentials(private_key.strip())

    if row:
        row.application_id_encrypted = encrypted_app_id
        row.private_key_encrypted = encrypted_private_key
    else:
        row = UserVonageCredential(
            user_id=user_id,
            application_id_encrypted=encrypted_app_id,
            private_key_encrypted=encrypted_private_key
        )
        db.add(row)

    db.flush()
    return row
