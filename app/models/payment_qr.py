import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred, relationship

from app.core.clock import utcnow
from app.database import Base
from app.services.media import public_media_url


class PaymentQr(Base):
    __tablename__ = "payment_qrs"
    __table_args__ = (
        CheckConstraint("NOT is_default OR is_active", name="ck_payment_qrs_default_active"),
        Index(
            "uq_payment_qrs_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(100), nullable=False)
    bank_name = Column(String(100))
    account_name = Column(String(100))
    currency = Column(String(3))
    blob = deferred(Column(LargeBinary, nullable=False))
    content_type = Column(String(32), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="payment_qrs")

    @property
    def url(self) -> str:
        return public_media_url("payment-qrs", self.id)
