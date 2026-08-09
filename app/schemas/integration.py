from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

TPlatform = Literal["messenger", "telegram"]


class IntegrationCreate(BaseModel):
    platform: TPlatform
    # Page id for Messenger, bot id for Telegram.
    external_id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    display_name: str | None = None


class IntegrationUpdate(BaseModel):
    display_name: str | None = None
    access_token: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None
    auto_reply: bool | None = None


class IntegrationOut(BaseModel):
    """Never carries the token — it is write-only by design.

    A stored page or bot token can read and send a seller's customer messages,
    so once submitted there is no read path back out through the API.
    """

    id: UUID
    platform: str
    external_id: str
    display_name: str | None
    is_active: bool
    auto_reply: bool
    created_at: datetime

    # Where the platform should deliver updates. Telegram needs the connection
    # id in the path; Messenger shares one URL across every page.
    webhook_url: str
    # Telegram only: registered with setWebhook so the bot's updates can be
    # told apart from anyone else posting to that path.
    webhook_secret: str | None = None

    model_config = {"from_attributes": True}


class ConnectionCheck(BaseModel):
    """Result of asking the platform whether a connection works."""

    ok: bool
    detail: str
