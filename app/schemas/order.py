from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class OrderItemCreate(BaseModel):
    product_id: UUID
    quantity: int
    # unit_price is intentionally omitted — the server uses the product's
    # current price as the source of truth (see endpoints/orders.py).


class OrderItemOut(BaseModel):
    id: UUID
    product_id: UUID
    quantity: int
    unit_price: Decimal
    subtotal: Decimal

    model_config = {"from_attributes": True}


class OrderCreate(BaseModel):
    customer_id: UUID
    conversation_id: UUID | None = None
    delivery_address: str | None = None
    notes: str | None = None
    items: list[OrderItemCreate]


class OrderUpdate(BaseModel):
    status: str | None = None
    delivery_address: str | None = None
    notes: str | None = None


class OrderOut(BaseModel):
    id: UUID
    user_id: UUID
    customer_id: UUID
    conversation_id: UUID | None
    status: str
    total_amount: Decimal
    currency: str
    delivery_address: str | None
    notes: str | None
    reservation_expires_at: datetime | None
    payment_status: str
    payment_method: str | None
    payment_receipt_attachment_id: UUID | None
    payment_confirmed_by_user_id: UUID | None
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut] = []

    model_config = {"from_attributes": True}


class CheckoutOut(BaseModel):
    """A hosted Stripe payment page for one order."""

    # Send this to the customer — in chat, or however the seller prefers.
    checkout_url: str
    session_id: str
    payment_status: str
