"""Password hashing (bcrypt) and JWT access-token helpers.

Isolated from FastAPI so it can be unit-tested and reused by the admin seed
script. Secrets are read from Settings (env / .env) and never hard-coded.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings

# bcrypt operates on <=72 bytes; longer inputs are truncated by the algorithm.
_BCRYPT_MAX_BYTES = 72


def hash_password(plain_password: str) -> str:
    """Return a bcrypt hash (salt embedded). Never store the plain password."""
    pw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Constant-time verification. Returns False on any malformed hash."""
    if not password_hash:
        return False
    try:
        pw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    *, subject: str, role: str, expires_minutes: int | None = None
) -> str:
    """Signed JWT carrying the user id (sub) and role. Short-lived by default."""
    settings = get_settings()
    minutes = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode + verify signature and expiry. Raises jwt.PyJWTError on failure."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
