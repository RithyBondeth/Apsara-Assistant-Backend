"""Issuing a dynamic KHQR for an order, and watching Bakong until it is paid."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.database import SessionLocal
from app.models.customer import Customer
from app.models.order import PAID, Order
from app.models.user import User
from app.services import customer_updates, khqr
from app.services.queue import enqueue, register
from app.services.seller_alerts import alert_paid, short_order

logger = logging.getLogger(__name__)

KHQR_CHECK = "khqr_check"

ISSUED = "issued"
KHQR_PAID = "paid"
EXPIRED = "expired"
MISMATCH = "mismatch"


class NotAvailable(ValueError):
    """KHQR is not set up for this shop, or not possible for this order."""


def enabled(seller: User) -> bool:
    return bool(seller.khqr_enabled and seller.bakong_account_id)


def image_url(order: Order) -> str:
    base = settings.API_BASE_URL.rstrip("/")
    return f"{base}/api/v1/media/khqr/{order.id}/{order.khqr_md5}.png"


def current(order: Order) -> bool:
    """Whether the order holds a code a customer can still pay."""
    return (order.khqr_status == ISSUED and order.khqr_payload is not None
            and order.khqr_expires_at is not None and order.khqr_expires_at > utcnow())


def issue(db: Session, order: Order, seller: User) -> Order:
    """Give the order a payable KHQR — the current one, or a fresh one — and
    queue the Bakong check. Committed by the caller."""
    if not enabled(seller):
        raise NotAvailable("Set up KHQR in Settings first")
    if order.payment_status == PAID:
        raise NotAvailable("This order is already paid")
    if order.status == "cancelled":
        raise NotAvailable("This order is cancelled")
    if current(order):
        return order

    expires = khqr.expiry()
    try:
        payload = khqr.build(
            account_id=seller.bakong_account_id,
            merchant_name=seller.khqr_merchant_name or seller.business_name or seller.full_name,
            merchant_city=seller.khqr_merchant_city or "Phnom Penh",
            amount=order.total_amount, currency=order.currency,
            bill_number=f"ORD-{short_order(order.id)}",
            merchant_id=seller.khqr_merchant_id, acquiring_bank=seller.khqr_acquiring_bank,
            expires_at_ms=int(expires.timestamp() * 1000),
        )
    except khqr.KhqrError as exc:
        raise NotAvailable(str(exc)) from exc
    order.khqr_payload = payload
    order.khqr_md5 = khqr.md5(payload)
    order.khqr_expires_at = expires
    order.khqr_status = ISSUED
    order.khqr_checked_at = None
    _schedule(db, order)
    return order


def _schedule(db: Session, order: Order) -> None:
    enqueue(db, KHQR_CHECK, {"order_id": str(order.id), "md5": order.khqr_md5},
            run_after=utcnow() + timedelta(seconds=settings.KHQR_POLL_SECONDS),
            max_attempts=1)


def _matches(order: Order, result: khqr.Check) -> bool:
    if result.currency != order.currency or result.amount is None:
        return False
    places = Decimal("1") if order.currency == "KHR" else Decimal("0.01")
    return result.amount.quantize(places) == Decimal(order.total_amount).quantize(places)


def apply(db: Session, order: Order, seller: User, result: khqr.Check) -> bool:
    """Record what Bakong said. Returns whether the order is now paid."""
    order.khqr_checked_at = utcnow()
    if result.outcome != khqr.FOUND:
        return False
    if not _matches(order, result):
        # Paid, but not what the code asked for — Bakong says so only if the
        # payer edited the amount. The seller decides; nothing is assumed.
        order.khqr_status = MISMATCH
        logger.warning("KHQR for order %s paid %s %s, expected %s %s", order.id,
                       result.amount, result.currency, order.total_amount, order.currency)
        return False
    if order.payment_status == PAID:
        order.khqr_status = KHQR_PAID
        return True
    now = utcnow()
    order.khqr_status = KHQR_PAID
    order.payment_status = PAID
    order.payment_method = "khqr"
    order.paid_at = now
    order.paid_amount = result.amount
    order.paid_currency = result.currency
    # Paid stock must never be released by reservation expiry.
    order.reservation_expires_at = None
    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    alert_paid(db, order, customer.name if customer else "")
    customer_updates.notify(db, order, customer_updates.PAID)
    return True


def check_now(db: Session, order: Order, seller: User) -> khqr.Check:
    token = khqr.api_token(seller)
    if not token:
        return khqr.Check(khqr.UNAVAILABLE, detail="No Bakong API token is set")
    result = khqr.check(order.khqr_md5, token)
    apply(db, order, seller, result)
    return result


@register(KHQR_CHECK)
def run_check(payload: dict) -> None:
    """One look at Bakong; schedules the next until paid or expired."""
    db = SessionLocal()
    try:
        order = (db.query(Order).filter(Order.id == payload["order_id"])
                 .with_for_update().first())
        # Superseded by a newer code, settled, or abandoned: nothing to watch.
        if (order is None or order.khqr_md5 != payload["md5"]
                or order.khqr_status != ISSUED or order.payment_status == PAID):
            return
        if order.khqr_expires_at <= utcnow():
            order.khqr_status = EXPIRED
            db.commit()
            return
        seller = db.query(User).filter(User.id == order.user_id).first()
        token = khqr.api_token(seller) if seller else None
        if not token:
            logger.warning("No Bakong token for order %s; not checking", order.id)
            return
        result = khqr.check(order.khqr_md5, token)
        if result.outcome == khqr.UNAVAILABLE:
            logger.warning("Bakong check for order %s: %s", order.id, result.detail)
        apply(db, order, seller, result)
        if order.khqr_status == ISSUED:
            _schedule(db, order)
        db.commit()
    finally:
        db.close()
