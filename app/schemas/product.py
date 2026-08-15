from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ProductCreate(BaseModel):
    name: str
    description: str | None = None
    price: Decimal
    stock: int = Field(default=0, ge=0)
    low_stock_threshold: int = Field(default=5, ge=0)
    image_url: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price: Decimal | None = None
    stock: int | None = Field(default=None, ge=0)
    low_stock_threshold: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    is_active: bool | None = None


class ProductOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None
    price: Decimal
    stock: int
    reserved_stock: int
    low_stock_threshold: int
    image_url: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class InventoryAdjustmentCreate(BaseModel):
    quantity_delta: int
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def delta_must_not_be_zero(self):
        if self.quantity_delta == 0:
            raise ValueError("Quantity adjustment must not be zero")
        return self


class InventoryMovementOut(BaseModel):
    id: UUID
    product_id: UUID | None
    product_name: str
    order_id: UUID | None
    created_by_user_id: UUID | None
    kind: str
    quantity_delta: int
    balance_after: int
    reserved_after: int
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExpiredReservationsOut(BaseModel):
    released_orders: int
    released_units: int
