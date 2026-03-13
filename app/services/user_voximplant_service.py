"""
User-specific Voximplant credentials service.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.auth import decrypt_twilio_credentials, encrypt_twilio_credentials
from app.models import UserVoximplantCredential


@dataclass
class VoximplantCredentials:
    account_id: str
    application_id: str
    service_account_email: str
    key_id: str
    private_key: str
    vox_app_id: int | None = None
    vox_rule_id: int | None = None
    vox_scenario_id: int | None = None
    provision_status: str = "pending"
    provision_error: str | None = None


def get_user_voximplant_credentials(db: Session, user_id: int) -> VoximplantCredentials | None:
    row = db.query(UserVoximplantCredential).filter(
        UserVoximplantCredential.user_id == user_id
    ).first()
    if not row:
        return None

    try:
        return VoximplantCredentials(
            account_id=decrypt_twilio_credentials(row.account_id_encrypted),
            application_id=decrypt_twilio_credentials(row.application_id_encrypted)
            if row.application_id_encrypted else "",
            service_account_email=decrypt_twilio_credentials(row.service_account_email_encrypted),
            key_id=decrypt_twilio_credentials(row.key_id_encrypted),
            private_key=decrypt_twilio_credentials(row.private_key_encrypted),
            vox_app_id=row.vox_app_id,
            vox_rule_id=row.vox_rule_id,
            vox_scenario_id=row.vox_scenario_id,
            provision_status=row.provision_status,
            provision_error=row.provision_error,
        )
    except Exception:
        return None


def has_user_voximplant_credentials(db: Session, user_id: int) -> bool:
    credentials = get_user_voximplant_credentials(db, user_id)
    if not credentials:
        return False
    return bool(
        credentials.account_id
        and credentials.service_account_email
        and credentials.key_id
        and credentials.private_key
    )


def upsert_user_voximplant_credentials(
    db: Session,
    user_id: int,
    *,
    account_id: str,
    application_id: str,
    service_account_email: str,
    key_id: str,
    private_key: str,
) -> UserVoximplantCredential:
    row = db.query(UserVoximplantCredential).filter(
        UserVoximplantCredential.user_id == user_id
    ).first()

    payload = {
        "account_id_encrypted": encrypt_twilio_credentials(account_id.strip()),
        "application_id_encrypted": encrypt_twilio_credentials(application_id.strip())
        if application_id.strip() else None,
        "service_account_email_encrypted": encrypt_twilio_credentials(service_account_email.strip()),
        "key_id_encrypted": encrypt_twilio_credentials(key_id.strip()),
        "private_key_encrypted": encrypt_twilio_credentials(private_key.strip()),
    }

    if row:
        for field, value in payload.items():
            setattr(row, field, value)
    else:
        row = UserVoximplantCredential(user_id=user_id, **payload)
        db.add(row)

    row.provision_status = "pending"
    row.provision_error = None
    db.flush()
    return row

