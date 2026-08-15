from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.inventory_movement import InventoryMovement
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.schemas.product import (
    ExpiredReservationsOut,
    InventoryAdjustmentCreate,
    InventoryMovementOut,
    ProductOut,
)
from app.services.inventory import move_stock, release_expired_reservations

router = APIRouter()


@router.get("/movements", response_model=list[InventoryMovementOut])
def list_movements(
    product_id: UUID | None = None,
    variant_id: UUID | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(InventoryMovement).filter(InventoryMovement.user_id == current_user.id)
    if product_id is not None:
        query = query.filter(InventoryMovement.product_id == product_id)
    if variant_id is not None:
        query = query.filter(InventoryMovement.variant_id == variant_id)
    return query.order_by(InventoryMovement.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/products/{product_id}/adjustments", response_model=ProductOut)
def adjust_product_stock(
    product_id: UUID,
    payload: InventoryAdjustmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.user_id == current_user.id)
        .with_for_update()
        .first()
    )
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product.id, ProductVariant.user_id == current_user.id)
        .order_by(ProductVariant.id)
        .with_for_update()
        .all()
    )
    if payload.variant_id is None:
        if len(variants) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a product variant to adjust",
            )
        variant = variants[0]
    else:
        variant = next((item for item in variants if item.id == payload.variant_id), None)
        if variant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    move_stock(
        db,
        product,
        variant,
        available_delta=payload.quantity_delta,
        kind="manual_adjustment",
        reason=payload.reason,
        actor_user_id=current_user.id,
    )
    db.commit()
    db.refresh(product)
    return product


@router.post("/release-expired", response_model=ExpiredReservationsOut)
def release_expired(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orders, units = release_expired_reservations(db, current_user.id)
    db.commit()
    return ExpiredReservationsOut(released_orders=orders, released_units=units)
