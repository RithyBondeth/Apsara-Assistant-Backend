from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.message import MessageOut


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    message_type: str = "text"  # text | image | voice


class ChatResponse(BaseModel):
    customer_message: MessageOut
    ai_message: MessageOut
