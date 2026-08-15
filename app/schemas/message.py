from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AttachmentOut(BaseModel):
    id: UUID
    file_url: str | None
    file_type: str | None
    file_name: str | None
    file_size: int | None
    review_status: str | None
    reviewed_at: datetime | None
    reviewed_by_user_id: UUID | None

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    message_type: str = "text"
    content: str = Field(min_length=1)


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_type: str
    message_type: str
    content: str | None
    created_at: datetime
    attachments: list[AttachmentOut] = []

    model_config = {"from_attributes": True}
