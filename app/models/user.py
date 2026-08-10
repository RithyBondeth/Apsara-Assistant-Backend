import uuid

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    business_name = Column(String)
    # The shop prices in one currency; products inherit it rather than each
    # carrying their own, which would allow an order to mix currencies and
    # make its total meaningless.
    currency = Column(String(3), nullable=False, server_default="USD")
    # The shop's payment QR — a KHQR, ABA or Wing code. Held as a URL rather
    # than a stored file because that is what both platforms want: they fetch
    # the image themselves, the same way product images already work. Empty
    # means the assistant simply never offers one.
    payment_qr_url = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    customers = relationship("Customer", back_populates="user")
    products = relationship("Product", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")
    orders = relationship("Order", back_populates="user")
    verification_codes = relationship(
        "VerificationCode", back_populates="user", cascade="all, delete-orphan"
    )
    platform_connections = relationship(
        "PlatformConnection", back_populates="user", cascade="all, delete-orphan"
    )
