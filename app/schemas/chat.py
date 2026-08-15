from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.message import MessageOut


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    message_type: str = "text"  # text | image | voice


class ChatResponse(BaseModel):
    customer_message: MessageOut
    ai_message: MessageOut
    # Present only when the assistant chose to send the shop's payment QR,
    # which is a separate message on Messenger and Telegram and so is one here
    # too rather than being folded into the reply.
    qr_message: MessageOut | None = None


class OrderDraftItemOut(BaseModel):
    product_id: UUID
    product_name: str
    variant_id: UUID
    variant_name: str
    variant_options: dict[str, str]
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    stock: int


class OrderDraftOut(BaseModel):
    customer_id: UUID
    conversation_id: UUID
    delivery_address: str | None
    notes: str | None
    items: list[OrderDraftItemOut]
    missing_fields: list[str]
    warnings: list[str]
