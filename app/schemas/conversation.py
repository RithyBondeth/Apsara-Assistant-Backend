from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.message import MessageOut


class ConversationCreate(BaseModel):
    customer_id: UUID
    platform: str


class ConversationUpdate(BaseModel):
    status: str | None = None


class ConversationOut(BaseModel):
    id: UUID
    user_id: UUID
    customer_id: UUID
    platform_connection_id: UUID | None
    platform: str
    source: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []
