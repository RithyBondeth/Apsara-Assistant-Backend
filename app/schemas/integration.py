from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

TPlatform = Literal["messenger", "telegram", "stripe"]


class IntegrationCreate(BaseModel):
    platform: TPlatform
    # Page id for Messenger, bot id for Telegram, account id (acct_...) for Stripe.
    external_id: str = Field(min_length=1)
    # Page token, bot token, or Stripe restricted secret key. Stored encrypted.
    access_token: str = Field(min_length=1)
    display_name: str | None = None
    # Stripe only: the signing secret (whsec_...) of the webhook endpoint the
    # seller added in their Stripe dashboard. Telegram's is generated here
    # instead, and Messenger authenticates with an app-level signature.
    webhook_secret: str | None = Field(default=None, min_length=1)


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
    # Telegram only: generated here and registered with setWebhook, so the
    # seller needs it to see what was configured. Stripe's signing secret is
    # the seller's own and never reads back out — same rule as access_token.
    webhook_secret: str | None = None

    model_config = {"from_attributes": True}


class ConnectionCheck(BaseModel):
    """Result of asking the platform whether a connection works."""

    ok: bool
    detail: str
