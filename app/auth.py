"""
Authentication utilities - JWT, password hashing, encryption
"""
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
    # Ensure the key is valid base64
    key = settings.ENCRYPTION_KEY
    if len(key) < 32:
        # Pad or generate a valid key for development
        key = key.ljust(32, '=')[:32]
        key = key.encode()
        import base64
        key = base64.urlsafe_b64encode(key)
    else:
        key = key.encode() if isinstance(key, str) else key
    return Fernet(key)


def encrypt_twilio_credentials(plain_text: str) -> str:
    """Encrypt Twilio credentials for storage"""
    fernet = get_fernet()
    return fernet.encrypt(plain_text.encode()).decode()


def decrypt_twilio_credentials(encrypted_text: str) -> str:
    """Decrypt Twilio credentials from storage"""
    fernet = get_fernet()
    return fernet.decrypt(encrypted_text.encode()).decode()
