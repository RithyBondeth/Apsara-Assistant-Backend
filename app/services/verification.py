"""Issuing and redeeming the single-use codes behind password reset and OTP login."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.models.verification_code import VerificationCode

PASSWORD_RESET = "password_reset"
LOGIN_OTP = "login_otp"


def hash_code(code: str) -> str:
    """Keyed hash of a code.

    HMAC rather than a bare digest: a six-digit OTP has only 10^6 possible
    values, so an unkeyed hash in a leaked database could be reversed by
    exhaustion. Keying with SECRET_KEY makes that useless without the key.
    """
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _recently_issued(db: Session, user: User, purpose: str) -> bool:
    """True if this account was sent a code moments ago.

    Stops a request loop turning the API into a mail bomb aimed at someone
    else's inbox.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=settings.CODE_REQUEST_COOLDOWN_SECONDS)
    return (
        db.query(VerificationCode)
        .filter(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == purpose,
            VerificationCode.created_at > cutoff,
        )
        .first()
        is not None
    )


def invalidate_outstanding(db: Session, user: User, purpose: str) -> None:
    """Consume every live code of this kind for the user."""
    now = datetime.utcnow()
    (
        db.query(VerificationCode)
        .filter(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == purpose,
            VerificationCode.consumed_at.is_(None),
        )
        .update({VerificationCode.consumed_at: now}, synchronize_session=False)
    )


def issue(db: Session, user: User, purpose: str) -> str | None:
    """Create a code for `user`, returning the plaintext to email.

    Returns None if one was issued too recently. Only one code of a given kind
    is ever live per user: issuing a new one retires the old, so an email that
    has been superseded stops working.
    """
    if _recently_issued(db, user, purpose):
        return None

    if purpose == LOGIN_OTP:
        code = f"{secrets.randbelow(1_000_000):06d}"
        lifetime = settings.OTP_EXPIRE_MINUTES
    else:
        code = secrets.token_urlsafe(32)
        lifetime = settings.PASSWORD_RESET_EXPIRE_MINUTES

    invalidate_outstanding(db, user, purpose)
    db.add(
        VerificationCode(
            user_id=user.id,
            purpose=purpose,
            code_hash=hash_code(code),
            expires_at=datetime.utcnow() + timedelta(minutes=lifetime),
        )
    )
    db.commit()
    return code


def _live(query):
    now = datetime.utcnow()
    return query.filter(
        VerificationCode.consumed_at.is_(None),
        VerificationCode.expires_at > now,
    )


def redeem_reset_token(db: Session, token: str) -> User | None:
    """Consume a password reset token, returning its owner."""
    record = _live(
        db.query(VerificationCode).filter(
            VerificationCode.code_hash == hash_code(token),
            VerificationCode.purpose == PASSWORD_RESET,
        )
    ).first()
    if record is None:
        return None

    record.consumed_at = datetime.utcnow()
    return db.query(User).filter(User.id == record.user_id).first()


def redeem_otp(db: Session, user: User, code: str) -> bool:
    """Consume a login OTP for `user`.

    Looked up by user rather than by hash — six digits are guessable, so the
    attempt ceiling is the real defence and it has to be per-code.
    """
    record = _live(
        db.query(VerificationCode).filter(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == LOGIN_OTP,
        )
    ).order_by(VerificationCode.created_at.desc()).first()
    if record is None:
        return False

    if record.attempts >= settings.OTP_MAX_ATTEMPTS:
        # Burn it rather than leaving a code that can be hammered further.
        record.consumed_at = datetime.utcnow()
        db.commit()
        return False

    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_code(code)):
        db.commit()
        return False

    record.consumed_at = datetime.utcnow()
    db.commit()
    return True
