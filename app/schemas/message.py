from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AttachmentOut(BaseModel):
    id: UUID
    file_url: str
    file_type: str | None
    file_name: str | None
    file_size: int | None

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    conversation_id: UUID
    sender_type: str
    message_type: str = "text"
    content: str | None = None


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_type: str
    message_type: str
    content: str | None
    created_at: datetime
    attachments: list[AttachmentOut] = []

    model_config = {"from_attributes": True}
