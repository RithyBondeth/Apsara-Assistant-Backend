import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    # What a vision model read off the image, so the seller checks a verdict
    # rather than squinting at a screenshot. Null until scanned; see
    # app/services/receipts.py for the statuses.
    ocr_status = Column(String(16))
    ocr_amount = Column(Numeric(12, 2))
    ocr_currency = Column(String(3))
    ocr_reference = Column(String(100), index=True)
    ocr_data = Column(JSONB)
    ocr_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)

    message = relationship("Message", back_populates="attachments")
