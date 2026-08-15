import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
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
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="conversations")
    customer = relationship("Customer", back_populates="conversations")
    platform_connection = relationship("PlatformConnection")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="conversation")
