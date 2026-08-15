import json
import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_product_variants_price_nonnegative"),
        CheckConstraint("stock >= 0", name="ck_product_variants_stock_nonnegative"),
        CheckConstraint("reserved_stock >= 0", name="ck_product_variants_reserved_nonnegative"),
        CheckConstraint(
            "low_stock_threshold >= 0",
            name="ck_product_variants_threshold_nonnegative",
        ),
        Index("uq_product_variants_option_signature", "product_id", "option_signature", unique=True),
        Index(
            "uq_product_variants_default",
            "product_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index(
            "uq_product_variants_sku",
            "user_id",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL"),
        ),
        Index(
            "uq_product_variants_barcode",
            "user_id",
            "barcode",
            unique=True,
            postgresql_where=text("barcode IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id = Column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    option_values = Column(JSONB, nullable=False, default=dict)
    option_signature = Column(String(1000), nullable=False)
    sku = Column(String(100))
    barcode = Column(String(100))
    price = Column(Numeric(12, 2), nullable=False)
    stock = Column(Integer, nullable=False, default=0)
    reserved_stock = Column(Integer, nullable=False, default=0)
    low_stock_threshold = Column(Integer, nullable=False, default=5)
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    product = relationship("Product", back_populates="variants")
    images = relationship("ProductImage", back_populates="variant")
    order_items = relationship("OrderItem", back_populates="variant")

    @property
    def name(self) -> str:
        if not self.option_values:
            return "Default"
        return " / ".join(str(self.option_values[key]) for key in sorted(self.option_values))

    @staticmethod
    def signature(option_values: dict[str, str]) -> str:
        return json.dumps(option_values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
