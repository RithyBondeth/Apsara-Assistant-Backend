from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session, undefer

from app.database import get_db
from app.models.order import Order
from app.models.payment_qr import PaymentQr
from app.models.product_image import ProductImage
from app.services import khqr
from app.services.media import read_or_503

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
    content = read_or_503(image) if image is not None else None
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return _image_response(content, image.content_type)


@router.get("/payment-qrs/{qr_id}")
def payment_qr_content(qr_id: UUID, db: Session = Depends(get_db)):
    qr = (
        db.query(PaymentQr)
        .options(undefer(PaymentQr.blob))
        .filter(PaymentQr.id == qr_id)
        .first()
    )
    content = read_or_503(qr) if qr is not None else None
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QR not found")
    return _image_response(content, qr.content_type)


@router.get("/khqr/{order_id}/{md5}.png")
def khqr_image(order_id: UUID, md5: str, db: Session = Depends(get_db)):
    """The order's current KHQR as an image, for Messenger and Telegram to fetch.

    Public like the payment QRs, but only at the MD5 of the code still on the
    order: an old link, or a guessed order id, finds nothing.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if order is None or not order.khqr_payload or order.khqr_md5 != md5:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QR not found")
    return _image_response(khqr.png(order.khqr_payload), "image/png")
