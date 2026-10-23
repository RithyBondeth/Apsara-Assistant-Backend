"""Rate limiting for password sign-in.

bcrypt makes a single guess expensive, but not expensive enough: at the cost
factor this app ships, an unthrottled `/auth/login` still accepts a steady
stream of guesses, and credential stuffing does not need many — it replays
passwords already known to belong to the address.

Two ceilings, in a sliding window:

  * per email — the one that matters. An attacker chooses the address they are
    trying to break into, so this is the counter they cannot escape.
  * per IP — a wider net for someone spraying one password across many
    addresses, which the per-email counter would never see. Evadable with
    enough hosts, and skipped entirely when the client address is unknown, so
    it is set loose enough not to catch a whole office behind one NAT.

Both are deliberately generous. The purpose is to make bulk guessing
impractical, not to lock a seller out of their own shop because they tried an
old password a few times.
"""

import logging
import secrets
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.models.login_attempt import LoginAttempt
from app.models.signup_attempt import SignupAttempt

logger = logging.getLogger(__name__)

# Roughly how many recorded failures between opportunistic sweeps of old rows.
_PRUNE_ONE_IN = 50


def normalise(email: str) -> str:
    """Key attempts on a canonical form so casing cannot multiply the budget."""
    return (email or "").strip().lower()


def client_ip(request) -> str | None:
    """The caller's address, as far as it can be trusted.

    A proxy header is only read when TRUST_PROXY_HEADERS says a proxy is in
    front and setting it. Reading it unconditionally would be worse than not
    having an IP ceiling at all: any caller could put a fresh value in the
    header on every request and never be counted twice.

    Even behind a trusted proxy, only the part the proxy wrote can be believed.
    X-Forwarded-For is appended to, not replaced, so its left-most entry is
    whatever the caller sent — the right-most is the address our proxy saw.
    A header the proxy overwrites outright (Railway's X-Real-IP) is read whole.
    """
    if settings.TRUST_PROXY_HEADERS:
        value = request.headers.get(settings.CLIENT_IP_HEADER)
        if value:
            return value.split(",")[-1].strip() or None
    return request.client.host if request.client else None


def _count_since(db: Session, cutoff, *, email=None, ip=None) -> int:
    query = db.query(func.count(LoginAttempt.id)).filter(LoginAttempt.created_at > cutoff)
    if email is not None:
        query = query.filter(LoginAttempt.email == email)
    if ip is not None:
        query = query.filter(LoginAttempt.ip == ip)
    return query.scalar() or 0


def too_many_attempts(db: Session, email: str, ip: str | None) -> bool:
    """Whether this sign-in should be refused without checking the password.

    Checked before the password is verified, so a throttled caller does not even
    pay for a bcrypt comparison — otherwise the endpoint stays a way to spend
    the server's CPU whether or not the guesses are counted.
    """
    if settings.LOGIN_MAX_ATTEMPTS <= 0:
        return False

    cutoff = utcnow() - settings.login_attempt_window

    if _count_since(db, cutoff, email=normalise(email)) >= settings.LOGIN_MAX_ATTEMPTS:
        logger.warning("Sign-in throttled for %s: per-account limit reached", normalise(email))
        return True

    if ip and settings.LOGIN_MAX_ATTEMPTS_PER_IP > 0:
        if _count_since(db, cutoff, ip=ip) >= settings.LOGIN_MAX_ATTEMPTS_PER_IP:
            logger.warning("Sign-in throttled for %s: per-address limit reached", ip)
            return True

    return False


def record_failure(db: Session, email: str, ip: str | None) -> None:
    db.add(LoginAttempt(email=normalise(email), ip=ip))
    db.commit()

    # The worker prunes on its lease interval, but JOB_RUNNER=inline has no
    # worker — and that is the single-process deployment least likely to have
    # anyone watching table sizes. Doing it here occasionally keeps the table
    # bounded in both modes. Failures are the only thing that grows it, so
    # tying the sweep to them means it runs exactly when it is needed.
    if secrets.randbelow(_PRUNE_ONE_IN) == 0:
        prune(db)


def clear_failures(db: Session, email: str) -> None:
    """Forget an account's failures after it signs in successfully."""
    db.query(LoginAttempt).filter(LoginAttempt.email == normalise(email)).delete(
        synchronize_session=False
    )
    db.commit()


def prune(db: Session) -> int:
    """Drop attempts too old to affect any decision.

    Nothing reads a row past the window, so without this the table is an
    append-only log of every failed sign-in the app has ever seen.
    """
    cutoff = utcnow() - settings.login_attempt_window
    removed = (
        db.query(LoginAttempt)
        .filter(LoginAttempt.created_at <= cutoff)
        .delete(synchronize_session=False)
    )
    removed += (
        db.query(SignupAttempt)
        .filter(SignupAttempt.created_at <= utcnow() - _signup_window())
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed


# ── Sign-up ──────────────────────────────────────────────────────────────────
# Every account gets a daily AI allowance and sends a verification email, so
# creating them in bulk costs money and mail reputation. Counted per client
# address; skipped when the address is unknown, like the sign-in ceiling.

def _signup_window():
    return timedelta(minutes=settings.REGISTER_WINDOW_MINUTES)


def too_many_signups(db: Session, ip: str | None) -> bool:
    if not ip or settings.REGISTER_MAX_PER_IP <= 0:
        return False
    count = (db.query(func.count(SignupAttempt.id))
             .filter(SignupAttempt.ip == ip,
                     SignupAttempt.created_at > utcnow() - _signup_window())
             .scalar() or 0)
    if count >= settings.REGISTER_MAX_PER_IP:
        logger.warning("Sign-up throttled for %s", ip)
        return True
    return False


def record_signup(db: Session, ip: str | None) -> None:
    """Counted with the account in the same commit, by the caller."""
    db.add(SignupAttempt(ip=ip))
