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
