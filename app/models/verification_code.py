import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base

# purpose: password_reset, login_otp


class VerificationCode(Base):
    """A single-use, expiring code issued to a user by email.

    Only a keyed hash of the code is stored, never the code itself — a database
    dump must not be enough to reset an account. The key is SECRET_KEY, which
    also stops an attacker precomputing all 10^6 six-digit OTP hashes.
    """

    __tablename__ = "verification_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = Column(String, nullable=False)
    code_hash = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="verification_codes")
