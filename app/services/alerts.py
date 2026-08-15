from uuid import UUID

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.database import SessionLocal
from app.models.operations import LowStockAlert
from app.models.platform_connection import PlatformConnection
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.services.email import send_email
from app.services.platforms import TELEGRAM, send_reply
from app.services.queue import enqueue, register


def evaluate_low_stock(db: Session, product: Product, variant: ProductVariant) -> LowStockAlert | None:
    """Open or resolve the single alert representing this low-stock episode."""
    open_alert = db.query(LowStockAlert).filter(
        LowStockAlert.variant_id == variant.id,
        LowStockAlert.resolved_at.is_(None),
    ).first()
    is_low = product.is_active and variant.is_active and variant.stock <= variant.low_stock_threshold
    if not is_low:
        if open_alert:
            open_alert.stock = variant.stock
            open_alert.resolved_at = utcnow()
        return None
    if open_alert:
        open_alert.stock = variant.stock
        open_alert.threshold = variant.low_stock_threshold
        return open_alert
    alert = LowStockAlert(
        user_id=product.user_id, product_id=product.id, variant_id=variant.id,
        product_name=product.name, variant_name=variant.name,
        stock=variant.stock, threshold=variant.low_stock_threshold,
    )
    db.add(alert)
    db.flush()
    enqueue(db, "low_stock_notification", {"alert_id": str(alert.id)})
    return alert


def scan_low_stock(db: Session, user_id: UUID) -> int:
    variants = db.query(ProductVariant).join(Product).filter(ProductVariant.user_id == user_id).all()
    opened = 0
    for variant in variants:
        before = db.query(LowStockAlert).filter(
            LowStockAlert.variant_id == variant.id, LowStockAlert.resolved_at.is_(None)
        ).first()
        alert = evaluate_low_stock(db, variant.product, variant)
        opened += int(alert is not None and before is None)
    return opened


@register("low_stock_notification")
def deliver_low_stock(payload: dict) -> None:
    db = SessionLocal()
    try:
        alert = db.query(LowStockAlert).filter(LowStockAlert.id == payload["alert_id"]).first()
        if not alert or alert.resolved_at:
            return
        user = db.query(User).filter(User.id == alert.user_id).first()
        if not user:
            return
        message = (
            f"Low stock: {alert.product_name} — {alert.variant_name}\n"
            f"{alert.stock} remaining (alert threshold: {alert.threshold})."
        )
        failed = []
        if user.low_stock_email_enabled and not alert.email_sent_at:
            if send_email(user.email, f"Low stock: {alert.product_name}", message):
                alert.email_sent_at = utcnow()
            else:
                failed.append("email")
        if (user.low_stock_telegram_enabled and user.low_stock_telegram_chat_id
                and not alert.telegram_sent_at):
            connection = db.query(PlatformConnection).filter(
                PlatformConnection.user_id == user.id,
                PlatformConnection.platform == TELEGRAM,
                PlatformConnection.is_active == True,
            ).order_by(PlatformConnection.created_at).first()
            if connection and send_reply(TELEGRAM, connection.access_token,
                                         user.low_stock_telegram_chat_id, message):
                alert.telegram_sent_at = utcnow()
            else:
                failed.append("Telegram")
        db.commit()
        if failed:
            raise RuntimeError(f"Low-stock delivery failed for {', '.join(failed)}")
    finally:
        db.close()
