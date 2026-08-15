from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class SupplierData(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = None
    notes: str | None = None
    is_active: bool = True


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SupplierOut(SupplierData):
    id: UUID
    created_at: datetime
    model_config = {"from_attributes": True}


class PurchaseItemCreate(BaseModel):
    variant_id: UUID
    ordered_quantity: int = Field(gt=0, le=1000000)
    unit_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class PurchaseOrderCreate(BaseModel):
    supplier_id: UUID
    items: list[PurchaseItemCreate] = Field(min_length=1, max_length=200)
    expected_at: datetime | None = None
    notes: str | None = None


class PurchaseOrderUpdate(BaseModel):
    status: str | None = None
    expected_at: datetime | None = None
    notes: str | None = None


class PurchaseItemOut(BaseModel):
    id: UUID
    product_id: UUID
    variant_id: UUID
    product_name: str
    variant_name: str
    variant_sku: str | None
    ordered_quantity: int
    received_quantity: int
    unit_cost: Decimal
    model_config = {"from_attributes": True}


class PurchaseOrderOut(BaseModel):
    id: UUID
    supplier_id: UUID
    status: str
    currency: str
    total_cost: Decimal
    expected_at: datetime | None
    notes: str | None
    ordered_at: datetime | None
    received_at: datetime | None
    created_at: datetime
    supplier: SupplierOut
    items: list[PurchaseItemOut]
    model_config = {"from_attributes": True}


class ReceiveItem(BaseModel):
    item_id: UUID
    quantity: int = Field(gt=0, le=1000000)


class ReceivePurchase(BaseModel):
    items: list[ReceiveItem] = Field(min_length=1, max_length=200)


class ReturnItemCreate(BaseModel):
    order_item_id: UUID
    quantity: int = Field(gt=0, le=1000000)
    restock_quantity: int = Field(default=0, ge=0, le=1000000)


class ReturnCreate(BaseModel):
    order_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    notes: str | None = None
    refund_amount: Decimal = Field(default=0, ge=0, max_digits=12, decimal_places=2)
    items: list[ReturnItemCreate] = Field(min_length=1, max_length=200)


class ReturnItemOut(BaseModel):
    id: UUID
    order_item_id: UUID
    product_id: UUID
    variant_id: UUID
    product_name: str
    variant_name: str
    quantity: int
    restock_quantity: int
    model_config = {"from_attributes": True}


class ReturnOut(BaseModel):
    id: UUID
    order_id: UUID
    status: str
    reason: str
    notes: str | None
    refund_amount: Decimal
    received_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime
    items: list[ReturnItemOut]
    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id: UUID
    product_id: UUID
    variant_id: UUID
    product_name: str
    variant_name: str
    stock: int
    threshold: int
    email_sent_at: datetime | None
    telegram_sent_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    model_config = {"from_attributes": True}


class BestSellerOut(BaseModel):
    product_id: UUID
    product_name: str
    variant_id: UUID
    variant_name: str
    units_sold: int
    revenue: Decimal


class ForecastOut(BaseModel):
    product_id: UUID
    product_name: str
    variant_id: UUID
    variant_name: str
    current_stock: int
    units_sold: int
    daily_velocity: float
    forecast_units: int
    days_of_cover: float | None
    suggested_reorder: int


class SalesDayOut(BaseModel):
    date: str
    orders: int
    units: int
    revenue: Decimal


class ReportOut(BaseModel):
    days: int
    forecast_days: int
    total_orders: int
    total_units: int
    total_revenue: Decimal
    best_sellers: list[BestSellerOut]
    forecast: list[ForecastOut]
    daily_sales: list[SalesDayOut]
