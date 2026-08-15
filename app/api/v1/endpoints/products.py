from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models.product import Product
from app.models.product_image import ProductImage
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.schemas.product import (
    ProductCreate,
    ProductImageOrder,
    ProductImageOut,
    ProductImageVariantUpdate,
    ProductOut,
    ProductUpdate,
    ProductVariantCreate,
    ProductVariantOut,
    ProductVariantUpdate,
)
from app.services.inventory import move_stock
from app.services.media import MAX_PRODUCT_IMAGES, read_image_upload
from app.services.variants import MAX_VARIANTS_PER_PRODUCT, clean_options, sync_product_totals

router = APIRouter()


@router.get("/", response_model=list[ProductOut])
def list_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Product).options(
        selectinload(Product.images), selectinload(Product.variants)
    ).filter(
        Product.user_id == current_user.id
    )
    if active_only:
        query = query.filter(Product.is_active == True)
    return query.order_by(Product.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = payload.model_dump(exclude={"variants"})
    opening_stock = data.pop("stock")
    requested_variants = payload.variants
    product = Product(**data, stock=0, user_id=current_user.id)
    db.add(product)
    db.flush()
    variants = requested_variants or [ProductVariantCreate(
        option_values={}, price=payload.price, stock=opening_stock,
        low_stock_threshold=payload.low_stock_threshold,
    )]
    created: list[tuple[ProductVariant, int]] = []
    for item in variants:
        options = clean_options(item.option_values)
        variant = ProductVariant(
            user_id=current_user.id,
            product_id=product.id,
            option_values=options,
            option_signature=ProductVariant.signature(options),
            sku=item.sku,
            barcode=item.barcode,
            price=item.price,
            stock=0,
            low_stock_threshold=item.low_stock_threshold,
            is_active=item.is_active,
            is_default=not options,
        )
        db.add(variant)
        created.append((variant, item.stock))
    try:
        db.flush()
        for variant, stock in created:
            if stock:
                move_stock(
                    db, product, variant,
                    available_delta=stock,
                    kind="opening_balance",
                    reason="Opening balance",
                    actor_user_id=current_user.id,
                )
        sync_product_totals(db, product)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Variant options, SKU, or barcode must be unique within the shop",
        ) from exc
    db.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = db.query(Product).options(
        selectinload(Product.images), selectinload(Product.variants)
    ).filter(
        Product.id == product_id, Product.user_id == current_user.id
    ).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _owned_product_for_media(db: Session, product_id: UUID, user_id: UUID) -> Product:
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.user_id == user_id)
        .with_for_update()
        .first()
    )
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _owned_variant(
    db: Session, product_id: UUID, variant_id: UUID, user_id: UUID, *, lock: bool = False
) -> tuple[Product, ProductVariant]:
    product = _owned_product_for_media(db, product_id, user_id)
    query = db.query(ProductVariant).filter(
        ProductVariant.id == variant_id,
        ProductVariant.product_id == product.id,
        ProductVariant.user_id == user_id,
    )
    variant = query.with_for_update().first() if lock else query.first()
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    return product, variant


@router.post(
    "/{product_id}/variants",
    response_model=ProductVariantOut,
    status_code=status.HTTP_201_CREATED,
)
def create_product_variant(
    product_id: UUID,
    payload: ProductVariantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = _owned_product_for_media(db, product_id, current_user.id)
    count = db.query(ProductVariant).filter(ProductVariant.product_id == product.id).count()
    if count >= MAX_VARIANTS_PER_PRODUCT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A product can have at most {MAX_VARIANTS_PER_PRODUCT} variants",
        )
    options = clean_options(payload.option_values)
    variant = ProductVariant(
        user_id=current_user.id,
        product_id=product.id,
        option_values=options,
        option_signature=ProductVariant.signature(options),
        sku=payload.sku,
        barcode=payload.barcode,
        price=payload.price,
        stock=0,
        low_stock_threshold=payload.low_stock_threshold,
        is_active=payload.is_active,
        is_default=not options,
    )
    db.add(variant)
    try:
        db.flush()
        if payload.stock:
            move_stock(
                db, product, variant,
                available_delta=payload.stock,
                kind="opening_balance",
                reason="Variant opening balance",
                actor_user_id=current_user.id,
            )
        sync_product_totals(db, product)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Variant options, SKU, or barcode already exist in this shop",
        ) from exc
    db.refresh(variant)
    return variant


@router.patch("/{product_id}/variants/{variant_id}", response_model=ProductVariantOut)
def update_product_variant(
    product_id: UUID,
    variant_id: UUID,
    payload: ProductVariantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product, variant = _owned_variant(db, product_id, variant_id, current_user.id, lock=True)
    data = payload.model_dump(exclude_unset=True)
    if "option_values" in data:
        options = clean_options(data.pop("option_values") or {})
        data["option_values"] = options
        data["option_signature"] = ProductVariant.signature(options)
        data["is_default"] = not options
    for field, value in data.items():
        setattr(variant, field, value)
    try:
        db.flush()
        sync_product_totals(db, product)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Variant options, SKU, or barcode already exist in this shop",
        ) from exc
    db.refresh(variant)
    return variant


@router.delete("/{product_id}/variants/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product_variant(
    product_id: UUID,
    variant_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product, variant = _owned_variant(db, product_id, variant_id, current_user.id, lock=True)
    count = db.query(ProductVariant).filter(ProductVariant.product_id == product.id).count()
    if count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A product must keep at least one variant",
        )
    if variant.stock or variant.reserved_stock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set a variant's stock to zero before deleting it, or deactivate it instead",
        )
    db.delete(variant)
    try:
        db.flush()
        sync_product_totals(db, product)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This variant belongs to an order and cannot be deleted. Deactivate it instead.",
        ) from exc


@router.post(
    "/{product_id}/images",
    response_model=list[ProductImageOut],
    status_code=status.HTTP_201_CREATED,
)
def upload_product_images(
    product_id: UUID,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = _owned_product_for_media(db, product_id, current_user.id)
    existing = (
        db.query(ProductImage)
        .filter(ProductImage.product_id == product.id)
        .order_by(ProductImage.position)
        .all()
    )
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose an image")
    if len(existing) + len(files) > MAX_PRODUCT_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A product can have at most {MAX_PRODUCT_IMAGES} images",
        )

    uploads = [read_image_upload(file) for file in files]
    for offset, (blob, content_type, file_name) in enumerate(uploads):
        db.add(ProductImage(
            user_id=current_user.id,
            product_id=product.id,
            blob=blob,
            content_type=content_type,
            file_name=file_name,
            file_size=len(blob),
            position=len(existing) + offset,
            is_primary=not existing and offset == 0,
        ))
    db.commit()
    return (
        db.query(ProductImage)
        .filter(ProductImage.product_id == product.id)
        .order_by(ProductImage.position)
        .all()
    )


@router.patch(
    "/{product_id}/images/{image_id}/variant",
    response_model=ProductImageOut,
)
def assign_product_image_variant(
    product_id: UUID,
    image_id: UUID,
    payload: ProductImageVariantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = _owned_product_for_media(db, product_id, current_user.id)
    image = db.query(ProductImage).filter(
        ProductImage.id == image_id,
        ProductImage.product_id == product.id,
        ProductImage.user_id == current_user.id,
    ).with_for_update().first()
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    if payload.variant_id is not None:
        variant = db.query(ProductVariant).filter(
            ProductVariant.id == payload.variant_id,
            ProductVariant.product_id == product.id,
            ProductVariant.user_id == current_user.id,
        ).first()
        if variant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    image.variant_id = payload.variant_id
    db.commit()
    db.refresh(image)
    return image


@router.put("/{product_id}/images/order", response_model=list[ProductImageOut])
def order_product_images(
    product_id: UUID,
    payload: ProductImageOrder,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = _owned_product_for_media(db, product_id, current_user.id)
    images = (
        db.query(ProductImage)
        .filter(ProductImage.product_id == product.id)
        .with_for_update()
        .all()
    )
    by_id = {image.id: image for image in images}
    if set(by_id) != set(payload.image_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image order must include every product image exactly once",
        )

    for image in images:
        image.is_primary = False
    db.flush()
    for position, image_id in enumerate(payload.image_ids):
        image = by_id[image_id]
        image.position = position
        image.is_primary = image_id == payload.primary_image_id
    db.commit()
    return [by_id[image_id] for image_id in payload.image_ids]


@router.delete("/{product_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product_image(
    product_id: UUID,
    image_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = _owned_product_for_media(db, product_id, current_user.id)
    images = (
        db.query(ProductImage)
        .filter(ProductImage.product_id == product.id)
        .order_by(ProductImage.position)
        .with_for_update()
        .all()
    )
    target = next((image for image in images if image.id == image_id), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    was_primary = target.is_primary
    db.delete(target)
    db.flush()
    remaining = [image for image in images if image.id != image_id]
    for position, image in enumerate(remaining):
        image.position = position
    if was_primary and remaining:
        remaining[0].is_primary = True
    db.commit()


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: UUID,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = db.query(Product).filter(
        Product.id == product_id, Product.user_id == current_user.id
    ).with_for_update().first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    data = payload.model_dump(exclude_unset=True)
    requested_stock = data.pop("stock", None)
    variants = db.query(ProductVariant).filter(
        ProductVariant.product_id == product.id
    ).order_by(ProductVariant.id).with_for_update().all()
    if requested_stock is not None and requested_stock != product.stock:
        if len(variants) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Adjust stock on a specific variant",
            )
        move_stock(
            db,
            product,
            variants[0],
            available_delta=requested_stock - variants[0].stock,
            kind="manual_adjustment",
            reason="Stock balance changed from product editor",
            actor_user_id=current_user.id,
        )
    if "price" in data and len(variants) == 1:
        variants[0].price = data["price"]
    elif "price" in data and len(variants) > 1 and data["price"] != product.price:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set price on each variant",
        )
    if "low_stock_threshold" in data and len(variants) == 1:
        variants[0].low_stock_threshold = data["low_stock_threshold"]
    elif (
        "low_stock_threshold" in data
        and len(variants) > 1
        and data["low_stock_threshold"] != product.low_stock_threshold
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set the low-stock threshold on each variant",
        )
    for field, value in data.items():
        setattr(product, field, value)
    sync_product_totals(db, product)
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    product = db.query(Product).filter(
        Product.id == product_id, Product.user_id == current_user.id
    ).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    db.delete(product)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This product belongs to an order and cannot be deleted. Archive it instead.",
        ) from exc
