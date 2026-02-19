"""
System settings service for global key/value settings stored in database.
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SystemSetting

settings = get_settings()

TWILIO_ACCOUNT_SID_KEY = "TWILIO_ACCOUNT_SID"
TWILIO_AUTH_TOKEN_KEY = "TWILIO_AUTH_TOKEN"


def get_setting(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    """Get a setting value by key."""
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        return row.value
    return default


def upsert_setting(db: Session, key: str, value: str) -> None:
    """Create or update a setting value."""
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
    else:
        row = SystemSetting(key=key, value=value)
        db.add(row)
    db.flush()


def get_twilio_credentials(db: Session) -> tuple[str, str]:
    """
    Get Twilio credentials from DB settings, falling back to environment values.
    """
    sid = get_setting(db, TWILIO_ACCOUNT_SID_KEY, settings.TWILIO_ACCOUNT_SID or "")
    token = get_setting(db, TWILIO_AUTH_TOKEN_KEY, settings.TWILIO_AUTH_TOKEN or "")
    return sid or "", token or ""
