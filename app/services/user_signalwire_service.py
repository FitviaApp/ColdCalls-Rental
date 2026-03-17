"""
User-specific SignalWire credentials service
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserSignalWireCredential


def _normalize_space_url(space_url: str) -> str:
    normalized = (space_url or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("https://"):
        normalized = normalized[len("https://"):]
    elif normalized.startswith("http://"):
        normalized = normalized[len("http://"):]
    return normalized.rstrip("/")


def get_user_signalwire_credentials(db: Session, user_id: int) -> tuple[str, str, str]:
    """Return decrypted SignalWire credentials (project_id, api_token, space_url) for a user."""
    row = db.query(UserSignalWireCredential).filter(
        UserSignalWireCredential.user_id == user_id
    ).first()
    if not row:
        return "", "", ""

    try:
        project_id = decrypt_twilio_credentials(row.project_id_encrypted)
        api_token = decrypt_twilio_credentials(row.api_token_encrypted)
        space_url = decrypt_twilio_credentials(row.space_url_encrypted)
    except Exception:
        return "", "", ""

    return project_id, api_token, _normalize_space_url(space_url)


def has_user_signalwire_credentials(db: Session, user_id: int) -> bool:
    """Check if user has valid SignalWire credentials saved."""
    project_id, api_token, space_url = get_user_signalwire_credentials(db, user_id)
    return bool(project_id and api_token and space_url)


def upsert_user_signalwire_credentials(
    db: Session,
    user_id: int,
    project_id: str,
    api_token: str,
    space_url: str,
) -> UserSignalWireCredential:
    """Create or update encrypted SignalWire credentials for a user."""
    row = db.query(UserSignalWireCredential).filter(
        UserSignalWireCredential.user_id == user_id
    ).first()

    normalized_space_url = _normalize_space_url(space_url)

    encrypted_project_id = encrypt_twilio_credentials(project_id.strip())
    encrypted_api_token = encrypt_twilio_credentials(api_token.strip())
    encrypted_space_url = encrypt_twilio_credentials(normalized_space_url)

    if row:
        row.project_id_encrypted = encrypted_project_id
        row.api_token_encrypted = encrypted_api_token
        row.space_url_encrypted = encrypted_space_url
    else:
        row = UserSignalWireCredential(
            user_id=user_id,
            project_id_encrypted=encrypted_project_id,
            api_token_encrypted=encrypted_api_token,
            space_url_encrypted=encrypted_space_url,
        )
        db.add(row)

    db.flush()
    return row
