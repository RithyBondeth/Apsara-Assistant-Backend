"""Issue and redeem single-use auth secrets (password reset, OTP login).

Raw secrets are returned to the caller (to email to the user) but never stored
— only their SHA-256 digest is persisted via :class:`app.models.auth_token.AuthToken`.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token
from app.models.auth_token import OTP_LOGIN, PASSWORD_RESET, AuthToken
from app.models.user import User


def _invalidate_outstanding(db: Session, user_id, purpose: str) -> None:
    """Drop any prior unused secrets of this purpose so only the newest is valid."""
    db.query(AuthToken).filter(
        AuthToken.user_id == user_id,
        AuthToken.purpose == purpose,
        AuthToken.used_at.is_(None),
    ).delete(synchronize_session=False)


def issue_password_reset(db: Session, user: User) -> str:
    """Create a reset token for ``user`` and return the raw (unhashed) value."""
    _invalidate_outstanding(db, user.id, PASSWORD_RESET)
    raw = secrets.token_urlsafe(32)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=PASSWORD_RESET,
            token_hash=hash_token(raw),
            expires_at=datetime.utcnow()
            + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        )
    )
    db.commit()
    return raw


def redeem_password_reset(db: Session, raw_token: str) -> User | None:
    """Consume a reset token, returning its user, or None if invalid/expired."""
    token = (
        db.query(AuthToken)
        .filter(
            AuthToken.purpose == PASSWORD_RESET,
            AuthToken.token_hash == hash_token(raw_token),
        )
        .first()
    )
    if not token or not token.is_usable():
        return None
    token.used_at = datetime.utcnow()
    return token.user


def issue_otp(db: Session, user: User) -> str:
    """Create a 6-digit login code for ``user`` and return it."""
    _invalidate_outstanding(db, user.id, OTP_LOGIN)
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=OTP_LOGIN,
            token_hash=hash_token(code),
            expires_at=datetime.utcnow() + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
        )
    )
    db.commit()
    return code


def verify_otp(db: Session, user: User, code: str) -> bool:
    """Check a login code for ``user``; consume it on success.

    A wrong guess increments the attempt counter and burns the code once it
    reaches ``OTP_MAX_ATTEMPTS`` so a low-entropy 6-digit code can't be brute-forced.
    """
    token = (
        db.query(AuthToken)
        .filter(
            AuthToken.user_id == user.id,
            AuthToken.purpose == OTP_LOGIN,
            AuthToken.used_at.is_(None),
        )
        .order_by(AuthToken.created_at.desc())
        .first()
    )
    if not token or not token.is_usable():
        return False

    if not secrets.compare_digest(token.token_hash, hash_token(code)):
        token.attempts += 1
        if token.attempts >= settings.OTP_MAX_ATTEMPTS:
            token.used_at = datetime.utcnow()
        db.commit()
        return False

    token.used_at = datetime.utcnow()
    db.commit()
    return True
