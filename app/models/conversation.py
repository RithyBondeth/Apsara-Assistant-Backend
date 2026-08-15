import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base

# status: open, closed, pending
# platform: messenger, telegram, tiktok, website


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    platform_connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    platform = Column(String, nullable=False)
    # "channel" is a real Messenger/Telegram thread; "rehearsal" is created
    # by a seller to test the assistant without contacting anybody.
    source = Column(String, nullable=False, server_default="rehearsal")
    status = Column(String, default="open")
    # Inbox ownership is separate from the channel's global auto-reply switch.
    # A seller can take over one sensitive thread without disabling the bot for
    # every other customer on that Messenger page or Telegram bot.
    handling_mode = Column(String, nullable=False, default="auto", server_default="auto")
    assigned_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_read_at = Column(DateTime)
    first_customer_message_at = Column(DateTime)
    first_response_at = Column(DateTime)
    last_customer_message_at = Column(DateTime)
    last_seller_message_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="conversations", foreign_keys=[user_id])
    customer = relationship("Customer", back_populates="conversations")
    platform_connection = relationship("PlatformConnection")
    assigned_user = relationship("User", foreign_keys=[assigned_user_id])
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="conversation")
    notes = relationship(
        "ConversationNote", back_populates="conversation", cascade="all, delete-orphan",
        order_by="ConversationNote.created_at",
    )
    tags = relationship(
        "ConversationTag", back_populates="conversation", cascade="all, delete-orphan",
        order_by="ConversationTag.name",
    )


class ConversationNote(Base):
    __tablename__ = "conversation_notes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    conversation = relationship("Conversation", back_populates="notes")
    author = relationship("User", foreign_keys=[author_user_id])


class ConversationTag(Base):
    __tablename__ = "conversation_tags"
    __table_args__ = (
        UniqueConstraint("conversation_id", "name", name="uq_conversation_tags_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = Column(String(40), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    conversation = relationship("Conversation", back_populates="tags")
