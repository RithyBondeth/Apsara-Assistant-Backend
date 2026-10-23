import uuid

from sqlalchemy import Column, DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.clock import utcnow
from app.database import Base


class SignupAttempt(Base):
    """One account created from an address — counted to cap bulk sign-ups."""

    __tablename__ = "signup_attempts"
    __table_args__ = (Index("ix_signup_attempts_ip_created_at", "ip", "created_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ip = Column(String)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
