import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.clock import utcnow
from app.database import Base


class InventoryMovement(Base):
    """Immutable audit entry for every change to available inventory."""

    __tablename__ = "inventory_movements"
    __table_args__ = (
        Index("ix_inventory_movements_user_created", "user_id", "created_at"),
        Index("ix_inventory_movements_product_created", "product_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id = Column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    product_name = Column(String, nullable=False)
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind = Column(String(32), nullable=False)
    quantity_delta = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=False)
    reserved_after = Column(Integer, nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    product = relationship("Product", back_populates="inventory_movements")
    order = relationship("Order", back_populates="inventory_movements")
