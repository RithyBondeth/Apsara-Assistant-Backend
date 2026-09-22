"""Telling the seller, on their phone, that something needs them.

The product's promise is that the shop keeps selling while the seller is
asleep. What was missing was the other half: when the assistant cannot answer,
when a customer sends a receipt, when a card payment lands — the seller found
out only by opening the inbox. These alerts go to the seller's own chat with
the Telegram bot they already connected, because that is the app a Cambodian
seller has open all day.

Delivery is a queued job, so a slow Telegram API never sits inside a webhook
or a request. The text is rendered at delivery time in the language the seller
currently uses.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.currency import format_amount
from app.database import SessionLocal
from app.models.conversation import Conversation
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.services.platforms import TELEGRAM, send_reply
from app.services.queue import enqueue, register

logger = logging.getLogger(__name__)

SELLER_ALERT = "seller_alert"

# The two switches a seller has. Low-stock alerts keep their own.
ATTENTION = "attention"
PAYMENT = "payment"

# How much of the customer's message the alert quotes. Enough to know what
# they want; short enough to read on a lock screen.
PREVIEW_CHARS = 120

# kind -> (category, {language: template}). Templates get `customer`,
# `preview`, `order`, `amount` and `url`; unused ones are simply not
# referenced. Khmer first in spirit — this is who the product is for.
TEMPLATES: dict[str, tuple[str, dict[str, str]]] = {
    "attention_escalated": (ATTENTION, {
        "en": "🔔 {customer} needs you\n“{preview}”\n"
              "The assistant could not answer this. Reply here: {url}",
        "km": "🔔 {customer} ត្រូវការអ្នក\n«{preview}»\n"
              "ជំនួយការមិនអាចឆ្លើយសំណួរនេះបានទេ។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    "attention_unanswered": (ATTENTION, {
        "en": "🔔 {customer} is waiting for a reply\n“{preview}”\n"
              "The assistant did not answer. Reply here: {url}",
        "km": "🔔 {customer} កំពុងរង់ចាំការឆ្លើយតប\n«{preview}»\n"
              "ជំនួយការមិនបានឆ្លើយទេ។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    "attention_manual": (ATTENTION, {
        "en": "💬 New message from {customer}\n“{preview}”\n"
              "You handle this conversation yourself. Reply here: {url}",
        "km": "💬 សារថ្មីពី {customer}\n«{preview}»\n"
              "ការសន្ទនានេះអ្នកជាអ្នកឆ្លើយផ្ទាល់។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    "attention_voice": (ATTENTION, {
        "en": "🎤 {customer} sent a voice message\n"
              "The assistant cannot listen to it. Reply here: {url}",
        "km": "🎤 {customer} បានផ្ញើសារជាសំឡេង\n"
              "ជំនួយការមិនអាចស្តាប់បានទេ។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    "attention_unsupported": (ATTENTION, {
        "en": "📎 {customer} sent a video, sticker or file\n"
              "The assistant cannot read it. Reply here: {url}",
        "km": "📎 {customer} បានផ្ញើវីដេអូ ស្ទីកឃ័រ ឬឯកសារ\n"
              "ជំនួយការមិនអាចអានបានទេ។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    "attention_photo": (ATTENTION, {
        "en": "📷 {customer} sent a photo\n"
              "The assistant does not answer photos. Reply here: {url}",
        "km": "📷 {customer} បានផ្ញើរូបភាព\n"
              "ជំនួយការមិនឆ្លើយតបរូបភាពទេ។ ឆ្លើយតបនៅទីនេះ៖ {url}",
    }),
    # `reading` is the receipt verdict when one could be produced, already
    # rendered in the seller's language with a leading newline; empty otherwise.
    "receipt_received": (PAYMENT, {
        "en": "🧾 {customer} sent a payment receipt\nOrder {order} · {amount}{reading}\n"
              "Check it and confirm the payment: {url}",
        "km": "🧾 {customer} បានផ្ញើវិក្កយបត្របង់ប្រាក់\nការបញ្ជាទិញ {order} · {amount}{reading}\n"
              "ពិនិត្យ និងបញ្ជាក់ការបង់ប្រាក់៖ {url}",
    }),
    "order_paid": (PAYMENT, {
        "en": "✅ Order {order} paid — {amount}\n{customer} paid by card. View it: {url}",
        "km": "✅ ការបញ្ជាទិញ {order} បានបង់ប្រាក់ — {amount}\n"
              "{customer} បានបង់តាមកាត។ មើល៖ {url}",
    }),
    "telegram_linked": (ATTENTION, {
        "en": "✅ Linked. Apsara will send alerts for {shop} here.",
        "km": "✅ បានភ្ជាប់ហើយ។ អាប់សារានឹងផ្ញើការជូនដំណឹងសម្រាប់ {shop} មកទីនេះ។",
    }),
}


def short_order(order_id) -> str:
    """The order as the seller sees it in the app: the first eight characters."""
    return str(order_id)[:8].upper()


def preview_of(text: str | None) -> str:
    text = " ".join((text or "").split())
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1].rstrip() + "…"


def conversation_url(conversation_id) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/chat?conversation={conversation_id}"


def order_url(order_id) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/orders?order={order_id}"


def render(kind: str, language: str, **params) -> str:
    _category, by_language = TEMPLATES[kind]
    template = by_language.get(language) or by_language["en"]
    return template.format_map(_Params(params))


class _Params(dict):
    """Leave a placeholder the caller did not supply visibly empty rather than
    raising: an alert with a blank is still an alert."""

    def __missing__(self, key):
        return ""


def telegram_target(db: Session, user: User) -> PlatformConnection | None:
    """The bot that can reach the seller, or None when they have not linked one.

    Any active Telegram connection will do — the seller's chat id is with
    whichever bot they opened the link from, but Telegram chat ids are the
    user's, not the bot's, so any of the seller's bots can message it.
    """
    if not user.telegram_chat_id:
        return None
    return (
        db.query(PlatformConnection)
        .filter(PlatformConnection.user_id == user.id,
                PlatformConnection.platform == TELEGRAM,
                PlatformConnection.is_active == True)
        .order_by(PlatformConnection.created_at)
        .first()
    )


def send_telegram(db: Session, user: User, text: str) -> bool:
    connection = telegram_target(db, user)
    if connection is None:
        return False
    return send_reply(TELEGRAM, connection.access_token, user.telegram_chat_id, text)


def enabled(user: User, category: str) -> bool:
    if category == ATTENTION:
        return user.attention_telegram_enabled
    if category == PAYMENT:
        return user.payment_telegram_enabled
    return False


# ── Raising alerts ────────────────────────────────────────────────────────────

def alert(db: Session, user_id, kind: str, **params) -> None:
    """Queue an alert. Committed by the caller with whatever prompted it.

    Cheap to call when the seller has nothing linked: the job is only written
    when it could be delivered, so a seller who never set Telegram up does not
    accumulate rows.
    """
    user = db.query(User).filter(User.id == user_id).first()
    category, _ = TEMPLATES[kind]
    if not user or not enabled(user, category) or telegram_target(db, user) is None:
        return
    enqueue(db, SELLER_ALERT, {"user_id": str(user_id), "kind": kind,
                               "params": {k: str(v) for k, v in params.items()}},
            max_attempts=3)


def flag_attention(db: Session, conversation: Conversation, kind: str,
                   customer_name: str, text: str | None) -> None:
    """Mark a thread as waiting on the seller and alert them — once.

    Only the transition raises an alert. A customer who sends five messages
    while the seller sleeps produces one notification, not five; the flag is
    cleared when the seller replies, and the next episode alerts again.
    """
    if conversation.needs_attention_at is not None:
        return
    conversation.needs_attention_at = utcnow()
    alert(db, conversation.user_id, kind,
          customer=customer_name, preview=preview_of(text),
          url=conversation_url(conversation.id))


def clear_attention(conversation: Conversation) -> None:
    conversation.needs_attention_at = None


def alert_receipt(db: Session, order, customer_name: str, *, reading: str = "") -> None:
    alert(db, order.user_id, "receipt_received",
          customer=customer_name, order=short_order(order.id),
          amount=format_amount(order.total_amount, order.currency),
          reading=reading, url=order_url(order.id))


def alert_paid(db: Session, order, customer_name: str) -> None:
    alert(db, order.user_id, "order_paid",
          customer=customer_name, order=short_order(order.id),
          amount=format_amount(order.total_amount, order.currency),
          url=order_url(order.id))


# ── Delivery ──────────────────────────────────────────────────────────────────

@register(SELLER_ALERT)
def deliver(payload: dict) -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == UUID(payload["user_id"])).first()
        kind = payload["kind"]
        category, _ = TEMPLATES[kind]
        # Re-checked at delivery: the seller may have switched the alert off,
        # or unlinked Telegram, between the trigger and now.
        if not user or not enabled(user, category):
            return
        connection = telegram_target(db, user)
        if connection is None:
            return
        text = render(kind, user.language, **payload.get("params", {}))
        if not send_reply(TELEGRAM, connection.access_token, user.telegram_chat_id, text):
            # Raising hands the job back to the queue for its retries.
            raise RuntimeError(f"Telegram alert {kind} not delivered to {user.id}")
    finally:
        db.close()
