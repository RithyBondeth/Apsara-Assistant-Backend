import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base

# platform: messenger, telegram


class PlatformConnection(Base):
    """A seller's Messenger page or Telegram bot.

    Webhooks arrive on one shared URL per platform, so this is what answers the
    only question that matters on the way in: whose customer is this? Messenger
    identifies the page in the payload; Telegram identifies nothing, so its
    webhook carries this row's id in the path.

    Tokens are stored encrypted — see app/core/crypto.py.
    """

    __tablename__ = "platform_connections"
    __table_args__ = (
        # One page or bot belongs to one seller. Without this, two sellers could
        # both claim a page and inbound messages would land in whichever row was
        # found first.
        UniqueConstraint("platform", "external_id", name="uq_platform_external_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    platform = Column(String, nullable=False)

    # Page id for Messenger; bot id for Telegram.
    external_id = Column(String, nullable=False, index=True)
    display_name = Column(String)

    # Encrypted at rest. Page access token / bot token.
    access_token = Column(String, nullable=False)

    # Telegram's X-Telegram-Bot-Api-Secret-Token. Messenger authenticates with
    # an app-level signature instead, so this stays null there.
    webhook_secret = Column(String)

    # Lets a seller silence the assistant without disconnecting: inbound
    # messages are still recorded, they just go unanswered.
    is_active = Column(Boolean, nullable=False, server_default="true")
    auto_reply = Column(Boolean, nullable=False, server_default="true")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="platform_connections")
