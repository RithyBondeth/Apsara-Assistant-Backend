from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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


class ReceiptOut(AttachmentOut):
    """A customer receipt as the order page sees it: the attachment, what was
    read off it, and how that compares with the order it is offered for."""

    ocr_status: str | None = None
    ocr_amount: Decimal | None = None
    ocr_currency: str | None = None
    ocr_reference: str | None = None
    ocr_data: dict | None = None
    ocr_at: datetime | None = None
    # match | amount_mismatch | currency_differs | duplicate | not_a_receipt |
    # unreadable, or None when not yet scanned.
    verdict: str | None = None
    read: str | None = None
    duplicate_of_order_id: UUID | None = None


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
