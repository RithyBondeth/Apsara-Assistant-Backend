import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class LowStockAlert(Base):
    __tablename__ = "low_stock_alerts"
    __table_args__ = (
        Index("uq_open_low_stock_alert", "variant_id", unique=True,
              postgresql_where=text("resolved_at IS NULL")),
        Index("ix_low_stock_alerts_user_created", "user_id", "created_at"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False)
    product_name = Column(String, nullable=False)
    variant_name = Column(String(500), nullable=False)
    stock = Column(Integer, nullable=False)
    threshold = Column(Integer, nullable=False)
    email_sent_at = Column(DateTime)
    telegram_sent_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    resolved_at = Column(DateTime)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (Index("ix_suppliers_user_name", "user_id", "name"),)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    email = Column(String(320))
    phone = Column(String(50))
    address = Column(Text)
    notes = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    currency = Column(String(3), nullable=False, default="USD")
    total_cost = Column(Numeric(12, 2), nullable=False, default=0)
    expected_at = Column(DateTime)
    notes = Column(Text)
    ordered_at = Column(DateTime)
    received_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    supplier = relationship("Supplier", back_populates="purchase_orders")
    items = relationship("PurchaseOrderItem", back_populates="purchase_order", cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        CheckConstraint("ordered_quantity > 0", name="ck_po_items_ordered_positive"),
        CheckConstraint("received_quantity >= 0 AND received_quantity <= ordered_quantity", name="ck_po_items_received_valid"),
        CheckConstraint("unit_cost >= 0", name="ck_po_items_cost_nonnegative"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False)
    product_name = Column(String, nullable=False)
    variant_name = Column(String(500), nullable=False)
    variant_sku = Column(String(100))
    ordered_quantity = Column(Integer, nullable=False)
    received_quantity = Column(Integer, nullable=False, default=0)
    unit_cost = Column(Numeric(12, 2), nullable=False)
    purchase_order = relationship("PurchaseOrder", back_populates="items")


class SalesReturn(Base):
    __tablename__ = "sales_returns"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="requested")
    reason = Column(Text, nullable=False)
    notes = Column(Text)
    refund_amount = Column(Numeric(12, 2), nullable=False, default=0)
    received_at = Column(DateTime)
    refunded_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    items = relationship("SalesReturnItem", back_populates="sales_return", cascade="all, delete-orphan")


class SalesReturnItem(Base):
    __tablename__ = "sales_return_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_return_items_quantity_positive"),
        CheckConstraint("restock_quantity >= 0 AND restock_quantity <= quantity", name="ck_return_items_restock_valid"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sales_return_id = Column(UUID(as_uuid=True), ForeignKey("sales_returns.id", ondelete="CASCADE"), nullable=False, index=True)
    order_item_id = Column(UUID(as_uuid=True), ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False)
    product_name = Column(String, nullable=False)
    variant_name = Column(String(500), nullable=False)
    quantity = Column(Integer, nullable=False)
    restock_quantity = Column(Integer, nullable=False, default=0)
    sales_return = relationship("SalesReturn", back_populates="items")
