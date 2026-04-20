"""
Application configuration using pydantic-settings
"""
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "ColdCalls Platform"
    SECRET_KEY: str = "change-me-in-production-min-32-chars"
    DEBUG: bool = False
    BASE_URL: str = "http://localhost:8000"  # Public URL for Twilio/SignalWire/Telnyx/Voximplant callbacks

    # Database
    DATABASE_URL: str = "sqlite:///./coldcalls.db"

    # JWT
    JWT_SECRET: str = "jwt-secret-change-me-min-32-chars"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 24

    # Encryption key for user voice-provider credentials (Fernet)
    ENCRYPTION_KEY: str = "encryption-key-must-be-32-url-safe-base64-chars"

    # Admin
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "change-me"

    # Cloudflare R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "coldcalls-audios"
    R2_PUBLIC_URL: str = ""

    # Etherscan (for USDT verification)
    ETHERSCAN_API_KEY: str = ""
    USDT_CONTRACT: str = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    USDT_WALLET_ADDRESS: str = ""

    # User limits
    MAX_USERS: int = 4

    # AI runtime defaults
    OPENAI_API_BASE: str = "https://api.openai.com/v1"
    OPENAI_DEFAULT_MODEL: str = "gpt-4o-mini"
    # Bounded so an OpenAI hiccup never exceeds the voice provider's webhook
    # timeout (~15s on Twilio/SignalWire) and cuts the call mid-turn.
    OPENAI_REQUEST_TIMEOUT_SECONDS: float = 8.0
    ELEVENLABS_TTS_MODEL: str = "eleven_flash_v2_5"
    ELEVENLABS_REQUEST_TIMEOUT_SECONDS: float = 8.0
    AI_MAX_AGENT_TURNS: int = 12
    AI_MAX_HISTORY_MESSAGES: int = 16
    AI_MAX_NO_INPUT_TURNS: int = 3
    AI_MAX_ASSISTANT_TEXT_CHARS: int = 320
    AI_GATHER_TIMEOUT_SECONDS: int = 6
    AI_GATHER_SPEECH_TIMEOUT_SECONDS: int = 1
    AI_GATHER_POST_PLAY_PAUSE_SECONDS: int = 0
    AI_HTTP_MAX_RETRIES: int = 1
    AI_HTTP_RETRY_BACKOFF_SECONDS: float = 0.3
    AI_MAX_CALL_DURATION_SECONDS: int = 600
    AI_MAX_CALL_COST_USD: float = 5.0
    AI_POLL_MAX_WAIT_SECONDS: int = 120
    AI_RUNTIME_ARTIFACT_TTL_SECONDS: int = 3600
    # Directory for TwiML audio + session files. /tmp is volatile on many
    # systems; allow overriding to a persistent path via env.
    AI_RUNTIME_DIR: str = "/tmp/coldcalls_ai_runtime"
    # Total budget for producing a follow-up TwiML response; must stay under
    # the voice provider's webhook timeout.
    AI_TURN_DEADLINE_SECONDS: float = 12.0

    @field_validator("DEBUG", mode="before")
    @classmethod
    def normalize_debug(cls, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "debug", "development", "dev"}:
            return True
        if normalized in {"0", "false", "no", "off", "release", "production", "prod"}:
            return False
        return value

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"
    }


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
