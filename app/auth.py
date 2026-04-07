"""
Authentication utilities - JWT, password hashing, encryption
"""
import base64
import hashlib
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRATION_HOURS)

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def get_fernet() -> Fernet:
    """Get Fernet instance for encryption/decryption"""
    key_value = settings.ENCRYPTION_KEY
    key_bytes = key_value.encode() if isinstance(key_value, str) else bytes(key_value)

    # Backward-compatible path for historical short keys used in this project.
    if len(key_bytes) < 32:
        legacy_seed = key_bytes.ljust(32, b"=")[:32]
        legacy_key = base64.urlsafe_b64encode(legacy_seed)
        return Fernet(legacy_key)

    # Accept a ready-to-use Fernet key when provided.
    try:
        decoded = base64.urlsafe_b64decode(key_bytes)
        if len(decoded) == 32:
            return Fernet(key_bytes)
    except Exception:
        pass

    # Derive a stable Fernet key from arbitrary text secrets.
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())
    return Fernet(derived_key)


def encrypt_twilio_credentials(plain_text: str) -> str:
    """Encrypt Twilio credentials for storage"""
    fernet = get_fernet()
    return fernet.encrypt(plain_text.encode()).decode()


def decrypt_twilio_credentials(encrypted_text: str) -> str:
    """Decrypt Twilio credentials from storage"""
    fernet = get_fernet()
    return fernet.decrypt(encrypted_text.encode()).decode()
