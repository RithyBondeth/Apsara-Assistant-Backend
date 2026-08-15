from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.inventory_movement import InventoryMovement
from app.models.order import UNPAID, Order
from app.models.product import Product

RESERVING_STATUSES = {"pending", "confirmed", "processing"}
FULFILLED_STATUSES = {"shipped", "delivered"}
RESERVATION_LIFETIME = timedelta(hours=24)


def reservation_deadline():
    return utcnow() + RESERVATION_LIFETIME


def move_stock(
    db: Session,
    product: Product,
    *,
    available_delta: int,
    reserved_delta: int = 0,
    kind: str,
    reason: str | None = None,
    order_id: UUID | None = None,
    actor_user_id: UUID | None = None,
) -> InventoryMovement:
    """Apply one locked stock mutation and append its immutable audit entry."""
    new_available = product.stock + available_delta
    new_reserved = product.reserved_stock + reserved_delta
    if new_available < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient stock for '{product.name}' (available: {product.stock})",
        )
    if new_reserved < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inventory reservation for '{product.name}' is inconsistent",
        )

    product.stock = new_available
    product.reserved_stock = new_reserved
    movement = InventoryMovement(
        user_id=product.user_id,
        product_id=product.id,
        product_name=product.name,
        order_id=order_id,
        created_by_user_id=actor_user_id,
        kind=kind,
        quantity_delta=available_delta,
        balance_after=new_available,
        reserved_after=new_reserved,
        reason=reason,
    )
    db.add(movement)
    return movement


def lock_order_products(db: Session, order: Order) -> dict[UUID, Product]:
    """Lock an order's products in stable order to avoid cross-order deadlocks."""
    product_ids = sorted({item.product_id for item in order.items}, key=str)
    products = (
        db.query(Product)
        .filter(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
        .all()
    )
    return {product.id: product for product in products}


def release_order_stock(
    db: Session,
    order: Order,
    *,
    kind: str,
    actor_user_id: UUID | None = None,
) -> int:
    released = 0
    was_reserved = order.status in RESERVING_STATUSES
    products = lock_order_products(db, order)
    for item in order.items:
        product = products.get(item.product_id)
        if product is None:
            continue
        move_stock(
            db,
            product,
            available_delta=item.quantity,
            reserved_delta=-item.quantity if was_reserved else 0,
            kind=kind,
            reason=f"Order {order.id}",
            order_id=order.id,
            actor_user_id=actor_user_id,
        )
        released += item.quantity
    order.reservation_expires_at = None
    return released


def release_expired_reservations(db: Session, user_id: UUID) -> tuple[int, int]:
    """Cancel expired pending orders and return their reservations atomically."""
    expired = (
        db.query(Order)
        .filter(
            Order.user_id == user_id,
            Order.status == "pending",
            # An issued payment link can complete asynchronously. Releasing
            # its stock first would let a later Stripe webhook create a paid,
            # oversold order; sellers can still cancel it explicitly.
            Order.payment_status == UNPAID,
            Order.reservation_expires_at.isnot(None),
            Order.reservation_expires_at <= utcnow(),
        )
        .with_for_update()
        .all()
    )
    released_units = 0
    for order in expired:
        released_units += release_order_stock(db, order, kind="reservation_expired")
        order.status = "cancelled"
    return len(expired), released_units
