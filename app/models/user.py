import uuid

from sqlalchemy import Boolean, Column, DateTime, String, Text
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
    # What the assistant knows about the shop beyond its catalogue. The
    # questions a Cambodian customer asks before buying — delivery fee to
    # their province, where to collect, whether they can exchange a size —
    # have nothing to do with any product row, so without these the model can
    # only promise to ask the seller. Free text: the seller writes it the way
    # they would answer a customer.
    shop_address = Column(Text)
    shop_hours = Column(Text)
    delivery_info = Column(Text)
    shop_policies = Column(Text)
    # Alerts, and the seller's language they are written in. The web app keeps
    # `language` in step with the language the seller picked there.
    language = Column(String(2), nullable=False, default="en", server_default="en")
    low_stock_email_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    low_stock_telegram_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    attention_telegram_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    payment_telegram_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    # The seller's own chat with their connected bot, where every Telegram
    # alert goes. Filled in by the bot itself when the seller opens the link
    # from Settings — nobody should have to find a numeric chat id by hand.
    telegram_chat_id = Column(String(100))
    telegram_chat_name = Column(String(200))
    telegram_linked_at = Column(DateTime)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    customers = relationship("Customer", back_populates="user")
    products = relationship("Product", back_populates="user")
    conversations = relationship(
        "Conversation", back_populates="user", foreign_keys="Conversation.user_id"
    )
    orders = relationship("Order", back_populates="user", foreign_keys="Order.user_id")
    verification_codes = relationship(
        "VerificationCode", back_populates="user", cascade="all, delete-orphan"
    )
    platform_connections = relationship(
        "PlatformConnection", back_populates="user", cascade="all, delete-orphan"
    )
    payment_qrs = relationship(
        "PaymentQr", back_populates="user", cascade="all, delete-orphan",
        order_by="PaymentQr.created_at",
    )
