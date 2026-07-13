"""Symmetric encryption for secrets stored at rest (integration tokens).

`EncryptedString` is a SQLAlchemy column type that transparently encrypts on
write and decrypts on read, so application code keeps working with plaintext
while the database only ever holds ciphertext.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key:
        # Derive a valid Fernet key from SECRET_KEY for dev/test convenience.
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


class EncryptedString(TypeDecorator):
    """A String column whose value is encrypted at rest via Fernet."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        try:
            return decrypt(value)
        except InvalidToken:
            # Tolerate pre-encryption (legacy plaintext) rows so enabling
            # encryption on an existing DB doesn't break reads.
            logger.warning("EncryptedString: value is not encrypted; returning as-is")
            return value
