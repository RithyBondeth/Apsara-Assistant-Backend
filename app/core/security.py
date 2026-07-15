from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

# bcrypt only considers the first 72 bytes of a password.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    digest = bcrypt.hashpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt())
    return digest.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8"))
    except ValueError:
        # Malformed / non-bcrypt hash stored — treat as a failed match
        return False


def create_access_token(subject: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def hash_token(raw: str) -> str:
    """SHA-256 of a single-use secret (reset token / OTP code) for storage at rest.

    We only ever persist this digest, so a database leak never exposes a usable
    token. SHA-256 (not bcrypt) is appropriate here: reset tokens are high-entropy,
    and OTP codes are additionally protected by short expiry and attempt limits.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
