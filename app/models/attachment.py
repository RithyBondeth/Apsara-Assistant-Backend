import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    file_url = Column(String, nullable=False)
    file_type = Column(String)
    file_name = Column(String)
    file_size = Column(Integer)
    created_at = Column(DateTime, default=utcnow)

    message = relationship("Message", back_populates="attachments")
