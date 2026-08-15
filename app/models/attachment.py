import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    # Outgoing QR images keep their public URL. Customer uploads are copied
    # into bounded private storage because platform download links expire and
    # Telegram's download URL contains the bot token.
    file_url = Column(String)
    blob = Column(LargeBinary)
    file_type = Column(String)
    file_name = Column(String)
    file_size = Column(Integer)
    review_status = Column(String)
    reviewed_at = Column(DateTime)
    reviewed_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=utcnow)

    message = relationship("Message", back_populates="attachments")
