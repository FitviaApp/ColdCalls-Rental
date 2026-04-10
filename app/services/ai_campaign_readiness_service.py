"""
Shared readiness checks for AI-agent campaigns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine
from app.models import VoiceProvider
from app.services.callback_url_service import validate_public_callback_url
from app.services.ai_realtime_session_service import is_redis_available
from app.services.user_elevenlabs_service import has_user_elevenlabs_credentials
from app.services.user_openai_service import has_user_openai_credentials
from app.services.user_twilio_service import has_user_twilio_credentials

settings = get_settings()

REQUIRED_AI_SCHEMA: dict[str, set[str]] = {
    "campaigns": {"campaign_mode", "ai_agent_id"},
    "campaign_numbers": {
        "ai_turn_count",
        "ai_no_input_turns",
        "ai_last_user_input",
        "ai_last_assistant_text",
        "ai_handoff_reason",
        "ai_runtime_error",
    },
}
REQUIRED_AI_TABLES = {
    "ai_agents",
    "user_openai_credentials",
    "user_elevenlabs_credentials",
}
PROVIDER_LABELS = {
    VoiceProvider.TWILIO.value: "Twilio",
}


@dataclass(frozen=True)
class AICampaignReadiness:
    ok: bool
    error: str | None
    schema_ready: bool
    missing_schema_items: tuple[str, ...]


def get_ai_schema_health(bind: Engine | None = None) -> dict[str, Any]:
    inspector = inspect(bind or engine)
    existing_tables = set(inspector.get_table_names())
    missing_items: list[str] = []

    for table_name in sorted(REQUIRED_AI_TABLES):
        if table_name not in existing_tables:
            missing_items.append(table_name)

    for table_name, required_columns in REQUIRED_AI_SCHEMA.items():
        if table_name not in existing_tables:
            missing_items.extend(f"{table_name}.{column_name}" for column_name in sorted(required_columns))
            continue
        existing_columns = {
            str(column.get("name") or "").strip()
            for column in inspector.get_columns(table_name)
        }
        for column_name in sorted(required_columns):
            if column_name not in existing_columns:
                missing_items.append(f"{table_name}.{column_name}")

    return {
        "ready": not missing_items,
        "missing_items": tuple(missing_items),
    }


def get_ai_campaign_readiness(
    db: Session,
    *,
    user_id: int,
    voice_provider: str,
    ai_agent=None,
    require_schema: bool = True,
    require_active_agent: bool = True,
) -> AICampaignReadiness:
    provider = (voice_provider or "").strip().lower()
    provider_label = PROVIDER_LABELS.get(provider, provider.title() or "provider")

    schema_health = get_ai_schema_health(db.get_bind() if db else None)
    if require_schema and not schema_health["ready"]:
        return AICampaignReadiness(
            ok=False,
            error=(
                "Database schema is outdated for AI campaigns. Restart the app and worker so "
                "init_db() can apply the latest AI schema updates."
            ),
            schema_ready=False,
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    try:
        validate_public_callback_url(settings.BASE_URL, provider_name=provider_label)
    except ValueError as exc:
        return AICampaignReadiness(
            ok=False,
            error=str(exc),
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if provider != VoiceProvider.TWILIO.value:
        return AICampaignReadiness(
            ok=False,
            error="AI agent campaigns currently require Twilio as the voice provider.",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if not is_redis_available():
        return AICampaignReadiness(
            ok=False,
            error="Redis is unavailable. AI campaigns require Redis for the realtime runtime.",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if not has_user_twilio_credentials(db, user_id):
        return AICampaignReadiness(
            ok=False,
            error="Please configure Twilio credentials in Settings first",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if not has_user_openai_credentials(db, user_id):
        return AICampaignReadiness(
            ok=False,
            error="Please configure OpenAI credentials in Settings first",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if not has_user_elevenlabs_credentials(db, user_id):
        return AICampaignReadiness(
            ok=False,
            error="Please configure ElevenLabs credentials in Settings first",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    if require_active_agent and (not ai_agent or not bool(getattr(ai_agent, "is_active", False))):
        return AICampaignReadiness(
            ok=False,
            error="Selected AI agent is inactive",
            schema_ready=bool(schema_health["ready"]),
            missing_schema_items=tuple(schema_health["missing_items"]),
        )

    return AICampaignReadiness(
        ok=True,
        error=None,
        schema_ready=bool(schema_health["ready"]),
        missing_schema_items=tuple(schema_health["missing_items"]),
    )
