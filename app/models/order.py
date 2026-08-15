import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base

# status: pending, confirmed, processing, shipped, delivered, cancelled
# payment_status: unpaid, pending, paid

UNPAID = "unpaid"
PAYMENT_PENDING = "pending"
PAID = "paid"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "payment_receipt_attachment_id",
            name="uq_orders_payment_receipt_attachment_id",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    status = Column(String, default="pending")
    total_amount = Column(Numeric(12, 2), default=0)
    # Copied from the seller when the order is placed. Without the snapshot,
    # a seller switching currency would silently reprice every past order.
    currency = Column(String(3), nullable=False, server_default="USD")
    delivery_address = Column(Text)
    notes = Column(Text)

    # Payment is tracked apart from `status`, not folded into it. The two answer
    # different questions — an order can be paid and not yet shipped, or
    # delivered and still unpaid on a cash-on-delivery sale — and a single
    # column would force one to overwrite the other.
    payment_status = Column(String, nullable=False, server_default=UNPAID)
    # Latest Stripe Checkout Session for this order. Indexed because the webhook
    # arrives knowing only this. Replaced when a seller reissues an expired link.
    stripe_session_id = Column(String, index=True)
    payment_method = Column(String)
    payment_receipt_attachment_id = Column(
        UUID(as_uuid=True), ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True
    )
    payment_confirmed_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="orders", foreign_keys=[user_id])
    customer = relationship("Customer", back_populates="orders")
    conversation = relationship("Conversation", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
