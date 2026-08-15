from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session, undefer

from app.database import get_db
from app.models.payment_qr import PaymentQr
from app.models.product_image import ProductImage

router = APIRouter()


def _image_response(blob: bytes, content_type: str) -> Response:
    return Response(
        content=blob,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/product-images/{image_id}")
def product_image_content(image_id: UUID, db: Session = Depends(get_db)):
    image = (
        db.query(ProductImage)
        .options(undefer(ProductImage.blob))
        .filter(ProductImage.id == image_id)
        .first()
    )
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return _image_response(image.blob, image.content_type)


@router.get("/payment-qrs/{qr_id}")
def payment_qr_content(qr_id: UUID, db: Session = Depends(get_db)):
    qr = (
        db.query(PaymentQr)
        .options(undefer(PaymentQr.blob))
        .filter(PaymentQr.id == qr_id)
        .first()
    )
    if qr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QR not found")
    return _image_response(qr.blob, qr.content_type)
