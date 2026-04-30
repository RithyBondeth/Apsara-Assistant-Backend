from __future__ import annotations

from pydantic import BaseModel

from app.schemas.message import MessageOut


class ChatRequest(BaseModel):
    message: str
    message_type: str = "text"  # text | image | voice


class ChatResponse(BaseModel):
    customer_message: MessageOut
    ai_message: MessageOut
