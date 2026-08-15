from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.product_variant import ProductVariant

MAX_VARIANTS_PER_PRODUCT = 100
MAX_OPTIONS_PER_VARIANT = 5


def clean_options(values: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = raw_name.strip()
        value = raw_value.strip()
        if not name or not value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Variant option names and values cannot be empty",
            )
        if len(name) > 50 or len(value) > 100:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Variant option names must be 50 characters or fewer and values 100 or fewer",
            )
        cleaned[name] = value
    if len(cleaned) > MAX_OPTIONS_PER_VARIANT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A variant can have at most {MAX_OPTIONS_PER_VARIANT} options",
        )
    return cleaned


def sync_product_totals(db: Session, product: Product) -> None:
    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product.id)
        .all()
    )
    active = [variant for variant in variants if variant.is_active]
    product.stock = sum(variant.stock for variant in active)
    product.reserved_stock = sum(variant.reserved_stock for variant in variants)
    product.low_stock_threshold = sum(variant.low_stock_threshold for variant in active)
    priced = active or variants
    if priced:
        product.price = min((variant.price for variant in priced), default=Decimal("0"))


def variant_label(variant: ProductVariant) -> str:
    return variant.name
