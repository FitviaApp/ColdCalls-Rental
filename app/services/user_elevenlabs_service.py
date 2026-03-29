"""
User-specific ElevenLabs credentials service.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserElevenLabsCredential


def get_user_elevenlabs_credentials(db: Session, user_id: int) -> str:
    """Return decrypted ElevenLabs API key for a user."""
    row = db.query(UserElevenLabsCredential).filter(
        UserElevenLabsCredential.user_id == user_id
    ).first()
    if not row:
        return ""

    try:
        return decrypt_twilio_credentials(row.api_key_encrypted)
    except Exception:
        return ""


def has_user_elevenlabs_credentials(db: Session, user_id: int) -> bool:
    return bool(get_user_elevenlabs_credentials(db, user_id))


def upsert_user_elevenlabs_credentials(
    db: Session,
    user_id: int,
    api_key: str,
) -> UserElevenLabsCredential:
    row = db.query(UserElevenLabsCredential).filter(
        UserElevenLabsCredential.user_id == user_id
    ).first()

    encrypted_api_key = encrypt_twilio_credentials(api_key.strip())

    if row:
        row.api_key_encrypted = encrypted_api_key
    else:
        row = UserElevenLabsCredential(
            user_id=user_id,
            api_key_encrypted=encrypted_api_key,
        )
        db.add(row)

    db.flush()
    return row
