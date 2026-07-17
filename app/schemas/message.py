from __future__ import annotations

from datetime import datetime
from typing import Literal
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
    """A seller's own reply, typed in the dashboard inbox.

    ``sender_type`` is fixed to "seller": the customer's messages arrive from
    their platform and the AI's come from the pipeline, so letting an authed
    caller pick any value here would only allow fabricating either side of a
    real customer's transcript.
    """

    sender_type: Literal["seller"] = "seller"
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
