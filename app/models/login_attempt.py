import uuid

from sqlalchemy import Column, DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.clock import utcnow
from app.database import Base


class LoginAttempt(Base):
    """One failed sign-in, kept just long enough to throttle the next.

    Only failures are recorded. A successful sign-in clears the account's rows,
    so someone who mistypes a password four times and then gets it right starts
    from a clean slate rather than carrying a penalty around for the rest of the
    window.

    In Postgres rather than in memory because the API runs as more than one
    process: a counter held in a worker's memory would give an attacker one full
    allowance per process, and would reset on every deploy.

    No foreign key to users. The email is stored as submitted (lowercased),
    whether or not an account exists — throttling only real accounts would make
    the 429 itself a membership oracle, undoing the care taken elsewhere in
    auth to keep registered addresses secret.
    """

    __tablename__ = "login_attempts"

    # Both ceilings count rows matching a key *within the window*, and the
    # sweep scans by age alone. Composite indexes rather than one per column so
    # each count is an index range read instead of a filter over every attempt
    # ever made against that address.
    __table_args__ = (
        Index("ix_login_attempts_email_created_at", "email", "created_at"),
        Index("ix_login_attempts_ip_created_at", "ip", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, nullable=False)
    # Best-effort. Null when the client address cannot be determined, and
    # untrustworthy when the app is behind a proxy that is not configured in
    # TRUST_PROXY_HEADERS — which is why the per-email ceiling, not this one,
    # is the defence that has to hold.
    ip = Column(String)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
