"""
Helpers for user voice provider availability.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import VoiceProvider
from app.services.user_signalwire_service import has_user_signalwire_credentials
from app.services.user_telnyx_service import has_user_telnyx_credentials
from app.services.user_twilio_service import has_user_twilio_credentials
from app.services.user_vonage_service import has_user_vonage_credentials
from app.services.user_voximplant_service import has_user_voximplant_credentials
from app.services.user_openai_service import has_user_openai_credentials
from app.services.user_elevenlabs_service import has_user_elevenlabs_credentials


PROVIDER_LABELS = {
    VoiceProvider.TWILIO.value: "Twilio",
    VoiceProvider.SIGNALWIRE.value: "SignalWire",
    VoiceProvider.TELNYX.value: "Telnyx",
    VoiceProvider.VONAGE.value: "Vonage",
    VoiceProvider.VOXIMPLANT.value: "Voximplant",
}


def supported_voice_providers() -> tuple[str, ...]:
    return (
        VoiceProvider.TWILIO.value,
        VoiceProvider.SIGNALWIRE.value,
        VoiceProvider.TELNYX.value,
        VoiceProvider.VONAGE.value,
        VoiceProvider.VOXIMPLANT.value,
    )


def provider_supports_press_1(provider: str) -> bool:
    provider = (provider or "").strip().lower()
    return provider in {
        VoiceProvider.TWILIO.value,
        VoiceProvider.SIGNALWIRE.value,
        VoiceProvider.VOXIMPLANT.value,
    }


def has_user_voice_provider_credentials(db: Session, user_id: int, provider: str) -> bool:
    """Check provider credentials for a specific user."""
    provider = (provider or "").strip().lower()

    if provider == VoiceProvider.TWILIO.value:
        return has_user_twilio_credentials(db, user_id)
    if provider == VoiceProvider.SIGNALWIRE.value:
        return has_user_signalwire_credentials(db, user_id)
    if provider == VoiceProvider.TELNYX.value:
        return has_user_telnyx_credentials(db, user_id)
    if provider == VoiceProvider.VONAGE.value:
        return has_user_vonage_credentials(db, user_id)
    if provider == VoiceProvider.VOXIMPLANT.value:
        return has_user_voximplant_credentials(db, user_id)
    return False


def has_any_user_voice_provider_credentials(db: Session, user_id: int) -> bool:
    """True when at least one provider is configured for the user."""
    return any(
        has_user_voice_provider_credentials(db, user_id, provider)
        for provider in supported_voice_providers()
    )


def get_user_voice_provider_status(db: Session, user_id: int) -> list[dict]:
    """List providers with configuration status for UI rendering."""
    providers = []
    for provider in supported_voice_providers():
        providers.append(
            {
                "value": provider,
                "label": PROVIDER_LABELS.get(provider, provider.title()),
                "configured": has_user_voice_provider_credentials(db, user_id, provider),
                "supports_press_1": provider_supports_press_1(provider),
            }
        )
    return providers


def has_user_ai_runtime_credentials(db: Session, user_id: int) -> bool:
    """True when the user can run Twilio-based AI-agent campaigns."""
    return (
        has_user_twilio_credentials(db, user_id)
        and has_user_openai_credentials(db, user_id)
        and has_user_elevenlabs_credentials(db, user_id)
    )
