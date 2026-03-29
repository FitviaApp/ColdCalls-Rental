"""
User-specific OpenAI credentials service.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserOpenAICredential


def get_user_openai_credentials(db: Session, user_id: int) -> tuple[str, str]:
    """Return decrypted OpenAI credentials (api_key, organization_id) for a user."""
    row = db.query(UserOpenAICredential).filter(
        UserOpenAICredential.user_id == user_id
    ).first()
    if not row:
        return "", ""

    try:
        api_key = decrypt_twilio_credentials(row.api_key_encrypted)
        organization_id = (
            decrypt_twilio_credentials(row.organization_id_encrypted)
            if row.organization_id_encrypted
            else ""
        )
    except Exception:
        return "", ""

    return api_key, organization_id


def has_user_openai_credentials(db: Session, user_id: int) -> bool:
    api_key, _organization_id = get_user_openai_credentials(db, user_id)
    return bool(api_key)


def upsert_user_openai_credentials(
    db: Session,
    user_id: int,
    api_key: str,
    organization_id: str = "",
) -> UserOpenAICredential:
    row = db.query(UserOpenAICredential).filter(
        UserOpenAICredential.user_id == user_id
    ).first()

    encrypted_api_key = encrypt_twilio_credentials(api_key.strip())
    encrypted_org = (
        encrypt_twilio_credentials(organization_id.strip())
        if organization_id.strip()
        else None
    )

    if row:
        row.api_key_encrypted = encrypted_api_key
        row.organization_id_encrypted = encrypted_org
    else:
        row = UserOpenAICredential(
            user_id=user_id,
            api_key_encrypted=encrypted_api_key,
            organization_id_encrypted=encrypted_org,
        )
        db.add(row)

    db.flush()
    return row
