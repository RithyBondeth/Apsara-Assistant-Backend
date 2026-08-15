from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class ProductVariantCreate(BaseModel):
    option_values: dict[str, str] = Field(default_factory=dict)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=100)
    price: Decimal = Field(ge=0)
    stock: int = Field(default=0, ge=0)
    low_stock_threshold: int = Field(default=5, ge=0)
    is_active: bool = True

    @field_validator("sku", "barcode")
    @classmethod
    def clean_identifier(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value else ""
        return cleaned or None


class ProductVariantUpdate(BaseModel):
    option_values: dict[str, str] | None = None
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=100)
    price: Decimal | None = Field(default=None, ge=0)
    low_stock_threshold: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("sku", "barcode")
    @classmethod
    def clean_identifier(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value else ""
        return cleaned or None


class ProductVariantOut(BaseModel):
    id: UUID
    product_id: UUID
    option_values: dict[str, str]
    name: str
    sku: str | None
    barcode: str | None
    price: Decimal
    stock: int
    reserved_stock: int
    low_stock_threshold: int
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProductImageOut(BaseModel):
    id: UUID
    url: str
    file_name: str
    file_size: int
    position: int
    is_primary: bool
    variant_id: UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProductCreate(BaseModel):
    name: str
    description: str | None = None
    price: Decimal
    stock: int = Field(default=0, ge=0)
    low_stock_threshold: int = Field(default=5, ge=0)
    image_url: str | None = None
    variants: list[ProductVariantCreate] | None = Field(default=None, min_length=1, max_length=100)


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
    images: list[ProductImageOut] = []
    variants: list[ProductVariantOut] = []

    model_config = {"from_attributes": True}


class ProductImageOrder(BaseModel):
    image_ids: list[UUID] = Field(min_length=1, max_length=8)
    primary_image_id: UUID

    @model_validator(mode="after")
    def validate_image_set(self):
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("Image ids must be unique")
        if self.primary_image_id not in self.image_ids:
            raise ValueError("Primary image must be included in image ids")
        return self


class ProductImageVariantUpdate(BaseModel):
    variant_id: UUID | None = None


class InventoryAdjustmentCreate(BaseModel):
    quantity_delta: int
    reason: str = Field(min_length=3, max_length=500)
    variant_id: UUID | None = None

    @model_validator(mode="after")
    def delta_must_not_be_zero(self):
        if self.quantity_delta == 0:
            raise ValueError("Quantity adjustment must not be zero")
        return self


class InventoryMovementOut(BaseModel):
    id: UUID
    product_id: UUID | None
    product_name: str
    variant_id: UUID | None
    variant_name: str | None
    variant_sku: str | None
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
