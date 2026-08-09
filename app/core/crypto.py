"""Symmetric encryption for third-party credentials held at rest.

A Messenger page token or a Telegram bot token is a live credential for someone
else's account: whoever holds one can read and send that seller's customer
messages. Storing them as plaintext columns would make a database dump — or a
stray query in a log — enough to take over every connected page.

The key is derived from SECRET_KEY so there is no second secret to distribute.
That does mean rotating SECRET_KEY makes stored tokens unreadable; sellers would
have to reconnect. Set PLATFORM_TOKEN_KEY to decouple the two.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class DecryptionError(RuntimeError):
    """Raised when stored ciphertext cannot be read with the current key."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.PLATFORM_TOKEN_KEY
    if not key:
        # Fernet wants 32 url-safe base64 bytes; SECRET_KEY is free-form.
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted. If SECRET_KEY or "
            "PLATFORM_TOKEN_KEY changed, affected accounts must be reconnected."
        ) from exc
