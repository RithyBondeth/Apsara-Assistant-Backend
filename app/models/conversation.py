import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base

# status: open, closed, pending
# platform: telegram, messenger, instagram, website


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String, nullable=False)
    status = Column(String, default="open")

    # False once a human takes over: inbound messages are still recorded, but
    # the webhooks stop auto-replying so the AI can't talk over the seller.
    ai_enabled = Column(Boolean, nullable=False, server_default="true", default=True)

    # Which bot/page this conversation arrived through, so a seller's reply
    # goes back out the same one. Null for conversations created before this
    # column existed or by hand in the dashboard — senders fall back to the
    # seller's first active integration for the platform.
    integration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_integrations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Does the seller need to look at this? ────────────────────────────────
    # True when the AI said it couldn't handle the question, or when it failed
    # outright. The seller can't be expected to re-read every thread the bot
    # already answered, so the ones it couldn't have to raise their hand.
    needs_attention = Column(Boolean, nullable=False, server_default="false", default=False)

    # When the seller last opened this thread. Null = never.
    last_seen_at = Column(DateTime)

    # Denormalised from the messages table so "is there something unread?" is a
    # column comparison rather than a per-conversation MAX() subquery on every
    # inbox render.
    last_customer_message_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations")
    customer = relationship("Customer", back_populates="conversations")
    integration = relationship("PlatformIntegration")

    @property
    def unread(self) -> bool:
        """Has the customer said something since the seller last looked?

        Deliberately keyed on the customer's messages only: the AI answering
        is not something the seller needs to re-read, and counting it would
        mark every thread unread forever.
        """
        if self.last_customer_message_at is None:
            return False
        if self.last_seen_at is None:
            return True
        return self.last_customer_message_at > self.last_seen_at
    # Ordered here so every load is chronological — the conversation-detail
    # endpoint joinedloads this relationship and would otherwise render the
    # thread in arbitrary database order.
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    orders = relationship("Order", back_populates="conversation")
