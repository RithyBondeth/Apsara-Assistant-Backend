import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base

# purpose values
PASSWORD_RESET = "password_reset"
OTP_LOGIN = "otp_login"


class AuthToken(Base):
    """A single-use secret issued for password reset or OTP login.

    Only the SHA-256 digest of the secret is stored (``token_hash``); the raw
    value lives only in the email we send. A row is valid while it is unused
    (``used_at`` is null) and unexpired (``expires_at`` in the future).
    """

    __tablename__ = "auth_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # PASSWORD_RESET | OTP_LOGIN
    purpose = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime)
    # Wrong-guess counter for OTP codes; burns the code once it hits the limit.
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="auth_tokens")

    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.utcnow()
