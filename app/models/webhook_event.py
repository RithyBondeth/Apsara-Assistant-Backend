import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class WebhookEvent(Base):
    """Ledger of processed inbound webhook events, for idempotency.

    Platforms redeliver a webhook if we don't ack in time. Recording the
    platform-unique event id (Telegram update_id, Messenger message.mid) lets
    us skip an event we've already handled instead of posting a duplicate reply.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("integration_id", "event_id", name="uq_webhook_event"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_integrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
