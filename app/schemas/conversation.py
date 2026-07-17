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
    # False pauses the AI so it stops auto-replying while a human handles this
    # conversation. Inbound messages are still recorded.
    ai_enabled: bool | None = None


class ConversationOut(BaseModel):
    id: UUID
    user_id: UUID
    customer_id: UUID
    platform: str
    status: str
    ai_enabled: bool
    # The AI couldn't handle this one (or failed) — the seller should look.
    needs_attention: bool
    # Computed on the model: the customer has said something since the seller
    # last opened the thread.
    unread: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []
