import uuid
from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AiUsage(Base):
    """How many assistant replies a seller has spent today.

    A counter rather than a scan of the messages table: this is checked on
    every inbound message, and inbound volume is not something the seller — or
    we — control.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_ai_usage_user_day"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    day = Column(Date, nullable=False, default=date.today)
    count = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow)
