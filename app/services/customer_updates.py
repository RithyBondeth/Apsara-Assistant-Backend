"""Telling the customer what happened to their order.

A customer who ordered over Messenger or Telegram used to hear nothing after
"the seller will confirm shortly" unless the seller remembered to write. Now
confirming, shipping, delivering or cancelling an order, and a confirmed
payment, each send them one short message in the thread they ordered from.

Queued, not sent in the request: the platform call can be slow or refused,
and neither should hold up the seller pressing a button.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.currency import format_dual
from app.database import SessionLocal
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import Order
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.services.platforms import MESSENGER, send_reply
from app.services.queue import enqueue, register
from app.services.seller_alerts import short_order

logger = logging.getLogger(__name__)

CUSTOMER_UPDATE = "customer_update"

PAID = "paid"
# Order statuses worth a message. "pending" and "processing" are the shop's
# own bookkeeping; a customer gains nothing from hearing about them.
STATUS_EVENTS = ("confirmed", "shipped", "delivered", "cancelled")
EVENTS = (*STATUS_EVENTS, PAID)

# Messenger only accepts a plain reply within 24 hours of the customer's last
# message. Past that, an order update has to carry this tag.
MESSENGER_WINDOW = timedelta(hours=24)
MESSENGER_TAG = "POST_PURCHASE_UPDATE"

_KHMER = re.compile(r"[ក-៿]")

TEMPLATES = {
    "confirmed": {
        "en": "✅ Your order #{order} is confirmed. Total: {total}. We'll let you know when it's on its way.",
        "km": "✅ ការបញ្ជាទិញ #{order} របស់អ្នកត្រូវបានបញ្ជាក់ហើយ។ សរុប៖ {total}។ យើងនឹងប្រាប់អ្នកនៅពេលដឹកចេញ។",
    },
    "shipped": {
        "en": "🚚 Your order #{order} is on its way!",
        "km": "🚚 ការបញ្ជាទិញ #{order} របស់អ្នកកំពុងដឹកជញ្ជូនទៅកាន់អ្នក!",
    },
    "delivered": {
        "en": "📦 Your order #{order} has been delivered. Thank you for shopping with {shop}!",
        "km": "📦 ការបញ្ជាទិញ #{order} របស់អ្នកបានដល់ហើយ។ សូមអរគុណដែលបានទិញពី {shop}!",
    },
    "cancelled": {
        "en": "Your order #{order} has been cancelled. If that's unexpected, just reply here.",
        "km": "ការបញ្ជាទិញ #{order} របស់អ្នកត្រូវបានលុបចោល។ បើមានចម្ងល់ សូមឆ្លើយតបមកសារនេះ។",
    },
    PAID: {
        "en": "💵 Payment received for order #{order}. Thank you!",
        "km": "💵 យើងបានទទួលការទូទាត់សម្រាប់ការបញ្ជាទិញ #{order} ហើយ។ សូមអរគុណ!",
    },
}


def notify(db: Session, order: Order, event: str) -> bool:
    """Queue `event` for the customer if it should be sent. Committed by the
    caller with the change that caused it. Returns whether it was queued."""
    if event not in EVENTS or order.conversation_id is None:
        return False
    if event in (order.customer_notified_events or []):
        return False
    seller = db.query(User).filter(User.id == order.user_id).first()
    if seller is None or not seller.customer_updates_enabled:
        return False
    conversation = db.query(Conversation).filter(Conversation.id == order.conversation_id).first()
    # A rehearsal thread has nobody on the other end.
    if conversation is None or conversation.source != "channel" \
            or conversation.platform_connection_id is None:
        return False
    enqueue(db, CUSTOMER_UPDATE, {"order_id": str(order.id), "event": event})
    return True


def customer_language(db: Session, conversation: Conversation) -> str:
    """Khmer if the customer has been writing Khmer script, else English."""
    said = (db.query(Message.content)
            .filter(Message.conversation_id == conversation.id,
                    Message.sender_type == "customer", Message.content.isnot(None))
            .order_by(Message.created_at.desc()).limit(5).all())
    return "km" if any(_KHMER.search(text or "") for (text,) in said) else "en"


def render(event: str, language: str, order: Order, seller: User) -> str:
    template = TEMPLATES[event].get(language) or TEMPLATES[event]["en"]
    return template.format(
        order=short_order(order.id),
        total=format_dual(order.total_amount, order.currency, seller.khr_rate or 4100),
        shop=seller.business_name or seller.full_name,
    )


@register(CUSTOMER_UPDATE)
def deliver(payload: dict) -> None:
    db = SessionLocal()
    try:
        order = (db.query(Order).filter(Order.id == payload["order_id"])
                 .with_for_update().first())
        event = payload["event"]
        if order is None or event in (order.customer_notified_events or []):
            return
        # The seller may have moved on since this was queued — confirmed, then
        # straight back to pending. Only what is still true is announced.
        if event in STATUS_EVENTS and order.status != event:
            return
        conversation = db.query(Conversation).filter(
            Conversation.id == order.conversation_id).first()
        connection = conversation and db.query(PlatformConnection).filter(
            PlatformConnection.id == conversation.platform_connection_id,
            PlatformConnection.is_active == True,  # noqa: E712
        ).first()
        customer = conversation and conversation.customer
        seller = db.query(User).filter(User.id == order.user_id).first()
        if not (connection and customer and seller):
            logger.info("Order %s update %s has no channel to go out on", order.id, event)
            return

        text = render(event, customer_language(db, conversation), order, seller)
        tag = None
        if connection.platform == MESSENGER:
            last = conversation.last_customer_message_at
            if last is None or utcnow() - last > MESSENGER_WINDOW:
                tag = MESSENGER_TAG
        if not send_reply(connection.platform, connection.access_token,
                          customer.platform_id, text, tag=tag):
            # Raised, so the queue retries with backoff; after the last
            # attempt it is recorded as failed and the seller still has the
            # order screen. The platform's reason is already in the log.
            raise RuntimeError(f"{connection.platform} refused the {event} update")

        db.add(Message(conversation_id=conversation.id, sender_type="assistant",
                       message_type="text", content=text))
        order.customer_notified_events = [*(order.customer_notified_events or []), event]
        conversation.updated_at = utcnow()
        db.commit()
    finally:
        db.close()
