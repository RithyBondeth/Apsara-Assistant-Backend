import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base

# sender_type: customer, assistant, seller
# message_type: text, image, voice, file


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_type = Column(String, nullable=False)
    message_type = Column(String, default="text")
    content = Column(Text)
    # The platform's own id for this message (Messenger mid, Telegram
    # update_id). Webhooks are retried on any non-2xx or timeout, so without a
    # key to recognise a replay the assistant would answer the same customer
    # twice and bill for it twice.
    external_id = Column(String, index=True)
    created_at = Column(DateTime, default=utcnow)

    conversation = relationship("Conversation", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")
