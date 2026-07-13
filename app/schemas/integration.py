from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class IntegrationCreate(BaseModel):
    platform: str  # telegram (messenger/tiktok in later phases)
    access_token: str  # bot token / page access token
    external_id: str | None = None
    secret_token: str | None = None


class IntegrationUpdate(BaseModel):
    access_token: str | None = None
    external_id: str | None = None
    secret_token: str | None = None
    is_active: bool | None = None


class IntegrationOut(BaseModel):
    id: UUID
    user_id: UUID
    platform: str
    external_id: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # access_token / secret_token are intentionally never returned
    model_config = {"from_attributes": True}


class WebhookRegisterOut(BaseModel):
    webhook_url: str
    ok: bool
    detail: str | None = None
