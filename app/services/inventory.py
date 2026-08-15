from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.inventory_movement import InventoryMovement
from app.models.order import UNPAID, Order
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.variants import sync_product_totals, variant_label

RESERVING_STATUSES = {"pending", "confirmed", "processing"}
FULFILLED_STATUSES = {"shipped", "delivered"}
RESERVATION_LIFETIME = timedelta(hours=24)


def reservation_deadline():
    return utcnow() + RESERVATION_LIFETIME


def move_stock(
    db: Session,
    product: Product,
    variant: ProductVariant,
    *,
    available_delta: int,
    reserved_delta: int = 0,
    kind: str,
    reason: str | None = None,
    order_id: UUID | None = None,
    actor_user_id: UUID | None = None,
) -> InventoryMovement:
    """Apply one locked stock mutation and append its immutable audit entry."""
    new_available = variant.stock + available_delta
    new_reserved = variant.reserved_stock + reserved_delta
    if new_available < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Insufficient stock for '{product.name} — {variant_label(variant)}' "
                f"(available: {variant.stock})"
            ),
        )
    if new_reserved < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inventory reservation for '{product.name} — {variant_label(variant)}' is inconsistent",
        )

    variant.stock = new_available
    variant.reserved_stock = new_reserved
    db.flush()
    sync_product_totals(db, product)
    movement = InventoryMovement(
        user_id=product.user_id,
        product_id=product.id,
        product_name=product.name,
        variant_id=variant.id,
        variant_name=variant_label(variant),
        variant_sku=variant.sku,
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


def lock_order_variants(db: Session, order: Order) -> dict[UUID, ProductVariant]:
    """Lock an order's variants in stable order to avoid cross-order deadlocks."""
    variant_ids = sorted({item.variant_id for item in order.items}, key=str)
    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.id.in_(variant_ids))
        .order_by(ProductVariant.id)
        .with_for_update()
        .all()
    )
    return {variant.id: variant for variant in variants}


def release_order_stock(
    db: Session,
    order: Order,
    *,
    kind: str,
    actor_user_id: UUID | None = None,
) -> int:
    released = 0
    was_reserved = order.status in RESERVING_STATUSES
    variants = lock_order_variants(db, order)
    for item in order.items:
        variant = variants.get(item.variant_id)
        if variant is None:
            continue
        move_stock(
            db,
            variant.product,
            variant,
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
