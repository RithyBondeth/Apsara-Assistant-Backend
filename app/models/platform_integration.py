import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.crypto import EncryptedString
from app.database import Base

# platform: messenger, telegram, tiktok, website


class PlatformIntegration(Base):
    """A seller's connection to an external messaging platform (their bot/page).

    Inbound webhooks are routed to a seller by looking up the integration, so
    each row holds the credentials needed to receive and reply on one channel.
    """

    __tablename__ = "platform_integrations"
    __table_args__ = (
        # A seller connects a given platform account only once
        UniqueConstraint("platform", "external_id", name="uq_platform_external_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String, nullable=False)
    # Platform-side account id: Telegram bot id, Facebook Page id, etc.
    external_id = Column(String, index=True)
    # Token used to call the platform's send API (bot token / page access token)
    access_token = Column(EncryptedString, nullable=False)
    # Shared secret we verify on every inbound webhook request.
    # Telegram: the setWebhook secret_token. Messenger: the verify token used
    # during the GET subscription handshake.
    secret_token = Column(EncryptedString)
    # Messenger only: the Facebook App Secret, used to verify the
    # X-Hub-Signature-256 HMAC on inbound POSTs.
    app_secret = Column(EncryptedString)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="integrations")
