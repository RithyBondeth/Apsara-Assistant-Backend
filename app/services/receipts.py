"""Reading a customer's payment receipt so the seller checks a verdict, not a screenshot.

Almost every sale here ends with a customer sending a screenshot from ABA,
ACLEDA, Wing or a Bakong KHQR app. The seller then opens the order, squints
at the amount, and presses confirm. This asks a vision model to read the
amount, currency and transaction reference off the image, compares them with
the order, and says one of a handful of things: it matches, the amount is
off, the currency differs, the same reference has been sent before, or the
picture is not a receipt at all.

The reading is advice. Confirming the payment is still the seller's tap —
a model can misread a 3 as an 8, and a doctored screenshot reads perfectly.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.currency import CURRENCIES, format_amount
from app.database import SessionLocal
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order import PAID, Order
from app.services import ai_service
from app.services.queue import enqueue, register
from app.services.quota import spend_receipt
from app.services.seller_alerts import alert_receipt

logger = logging.getLogger(__name__)

RECEIPT_OCR = "receipt_ocr"

# ocr_status values.
READ = "read"              # the model returned a reading
NOT_RECEIPT = "not_receipt"  # it read the image, and it is not a receipt
FAILED = "failed"          # model error, quota, or not configured

# Verdicts, relative to one order.
MATCH = "match"
AMOUNT_MISMATCH = "amount_mismatch"
CURRENCY_DIFFERS = "currency_differs"
DUPLICATE = "duplicate"
NOT_A_RECEIPT = "not_a_receipt"
UNREADABLE = "unreadable"


class ReceiptReading(BaseModel):
    """The model-facing shape. Every field is required but nullable, which is
    what strict structured output wants; None means "not on the receipt"."""

    model_config = ConfigDict(extra="forbid")

    is_receipt: bool
    amount: float | None
    currency: str | None
    reference: str | None
    bank: str | None
    payer: str | None
    payee: str | None
    paid_at: str | None


def build_receipt_messages(image_bytes: bytes, content_type: str) -> list[dict]:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    system = """You read payment receipts from Cambodian banking and wallet apps —
ABA, ACLEDA, Wing, TrueMoney, Bakong KHQR, Canadia, Prince and similar — sent
as screenshots by customers. They may be in Khmer, English, or both.

Report exactly what is printed; never infer or round. `amount` is the amount
transferred, as a number. `currency` is "USD" or "KHR" as shown ($, USD,
dollar → USD; ៛, KHR, riel, រៀល → KHR), or null if not shown. `reference` is
the transaction id / reference number / trx id, copied character for
character. `bank` is the app or bank name. `payer` is the sender's name,
`payee` the receiver's. `paid_at` is the date and time as printed. If the
image is not a payment receipt at all — a product photo, a chat screenshot,
anything else — set is_receipt to false and every other field to null."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": "Read this receipt."},
            {"type": "image_url", "image_url": {
                "url": f"data:{content_type};base64,{encoded}", "detail": "auto",
            }},
        ]},
    ]


def read_receipt(image_bytes: bytes, content_type: str) -> ReceiptReading:
    """Ask the model. Raises `ai_service.AIError` on any failure."""
    if not settings.OPENAI_API_KEY:
        raise ai_service.AIError("Receipt reading is not configured. Set OPENAI_API_KEY.")
    schema = ReceiptReading.model_json_schema()
    try:
        response = ai_service._client().chat.completions.create(
            model=settings.OPENAI_VISION_MODEL,
            messages=build_receipt_messages(image_bytes, content_type),
            temperature=0,
            max_tokens=400,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "receipt", "strict": True, "schema": schema},
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise ai_service.AIError("The model returned nothing for the receipt.")
        return ReceiptReading.model_validate_json(content)
    except ai_service.AIError:
        raise
    except (OpenAIError, ValidationError, AttributeError, IndexError, TypeError) as exc:
        logger.exception("Receipt reading failed")
        raise ai_service.AIError("The receipt could not be read right now.") from exc


# ── Recording a reading ───────────────────────────────────────────────────────

def _amount(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        # Half up, as a bank would print it — not the banker's rounding
        # Decimal defaults to.
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _currency(value: str | None) -> str | None:
    if not value:
        return None
    code = value.strip().upper()
    return code if code in CURRENCIES else None


def apply_reading(attachment: Attachment, reading: ReceiptReading) -> None:
    attachment.ocr_status = READ if reading.is_receipt else NOT_RECEIPT
    attachment.ocr_amount = _amount(reading.amount) if reading.is_receipt else None
    attachment.ocr_currency = _currency(reading.currency) if reading.is_receipt else None
    attachment.ocr_reference = (reading.reference or "").strip()[:100] or None
    attachment.ocr_data = reading.model_dump()
    attachment.ocr_at = utcnow()


def mark_failed(attachment: Attachment, reason: str) -> None:
    attachment.ocr_status = FAILED
    attachment.ocr_amount = None
    attachment.ocr_currency = None
    attachment.ocr_reference = None
    attachment.ocr_data = {"error": reason}
    attachment.ocr_at = utcnow()


def scan(db: Session, attachment: Attachment, user_id) -> None:
    """Read one attachment and record the outcome on it. Never raises: a
    failed read is itself a recorded outcome the seller can see and retry."""
    if attachment.blob is None or not (attachment.file_type or "").startswith("image/"):
        mark_failed(attachment, "not an image")
        return
    if settings.AI_DAILY_RECEIPT_LIMIT <= 0:
        mark_failed(attachment, "receipt scanning is disabled")
        return
    if not spend_receipt(db, user_id):
        mark_failed(attachment, "daily receipt limit reached")
        return
    try:
        apply_reading(attachment, read_receipt(attachment.blob, attachment.file_type))
    except ai_service.AIError as exc:
        mark_failed(attachment, str(exc))


# ── Judging a reading against an order ────────────────────────────────────────

@dataclass(frozen=True)
class Assessment:
    verdict: str | None       # None when the attachment has not been scanned
    read: str | None          # the amount as read, formatted, for display
    duplicate_of_order_id: UUID | None = None


def duplicate_of(db: Session, attachment: Attachment, user_id) -> Attachment | None:
    """An earlier receipt of this seller's carrying the same reference.

    The same transfer offered for two orders is the fraud a small shop
    actually meets. Matched on the reference only, across every conversation
    the seller has, and regardless of whether the earlier one was accepted —
    two receipts with one reference are one payment either way. Only earlier
    ones count: the first to arrive is the original, and stays clean when a
    copy turns up later.
    """
    if not attachment.ocr_reference:
        return None
    return (
        db.query(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(
            Conversation.user_id == user_id,
            Attachment.id != attachment.id,
            Attachment.ocr_reference == attachment.ocr_reference,
            Attachment.created_at <= attachment.created_at,
        )
        .order_by(Attachment.created_at)
        .first()
    )


def assess(db: Session, attachment: Attachment, order: Order) -> Assessment:
    if attachment.ocr_status is None:
        return Assessment(None, None)
    if attachment.ocr_status == FAILED:
        return Assessment(UNREADABLE, None)
    if attachment.ocr_status == NOT_RECEIPT:
        return Assessment(NOT_A_RECEIPT, None)

    currency = attachment.ocr_currency or order.currency
    read = (format_amount(attachment.ocr_amount, currency)
            if attachment.ocr_amount is not None else None)

    earlier = duplicate_of(db, attachment, order.user_id)
    if earlier is not None:
        used_for = (
            db.query(Order.id)
            .filter(Order.payment_receipt_attachment_id == earlier.id)
            .first()
        )
        return Assessment(DUPLICATE, read, used_for[0] if used_for else None)

    if attachment.ocr_amount is None:
        return Assessment(UNREADABLE, None)
    if attachment.ocr_currency and attachment.ocr_currency != order.currency:
        return Assessment(CURRENCY_DIFFERS, read)
    if attachment.ocr_amount == Decimal(order.total_amount):
        return Assessment(MATCH, read)
    return Assessment(AMOUNT_MISMATCH, read)


# ── The one line the Telegram alert carries ───────────────────────────────────

VERDICT_LINES: dict[str, dict[str, str]] = {
    MATCH: {
        "en": "✅ Receipt reads {read} — matches",
        "km": "✅ វិក្កយបត្រអាន {read} — ត្រូវគ្នា",
    },
    AMOUNT_MISMATCH: {
        "en": "⚠️ Receipt reads {read}, the order is {amount}",
        "km": "⚠️ វិក្កយបត្រអាន {read} តែការបញ្ជាទិញ {amount}",
    },
    CURRENCY_DIFFERS: {
        "en": "⚠️ Receipt reads {read} — different currency, check the rate",
        "km": "⚠️ វិក្កយបត្រអាន {read} — រូបិយប័ណ្ណខុសគ្នា សូមពិនិត្យអត្រា",
    },
    DUPLICATE: {
        "en": "🚫 Same reference as a receipt you were already sent",
        "km": "🚫 លេខយោងដូចវិក្កយបត្រដែលបានផ្ញើមកអ្នករួចហើយ",
    },
    NOT_A_RECEIPT: {
        "en": "The photo does not look like a payment receipt",
        "km": "រូបភាពមិនមើលទៅដូចវិក្កយបត្របង់ប្រាក់ទេ",
    },
    UNREADABLE: {
        "en": "Could not read the receipt — check it yourself",
        "km": "មិនអាចអានវិក្កយបត្របានទេ — សូមពិនិត្យដោយខ្លួនឯង",
    },
}


def verdict_line(assessment: Assessment, order: Order, language: str) -> str:
    if assessment.verdict is None:
        return ""
    template = VERDICT_LINES[assessment.verdict]
    return (template.get(language) or template["en"]).format(
        read=assessment.read or "?",
        amount=format_amount(order.total_amount, order.currency),
    )


# ── The queued job: read, then tell the seller ────────────────────────────────

def queue_scan(db: Session, order: Order, attachment_ids: list, customer_name: str) -> None:
    """Scan these receipts and then alert the seller, in that order, so the
    alert can carry the verdict. Committed by the caller."""
    enqueue(db, RECEIPT_OCR, {
        "order_id": str(order.id),
        "attachment_ids": [str(a) for a in attachment_ids],
        "customer": customer_name,
    })


@register(RECEIPT_OCR)
def run_scan(payload: dict) -> None:
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == UUID(payload["order_id"])).first()
        if order is None:
            return
        attachments = (
            db.query(Attachment)
            .filter(Attachment.id.in_([UUID(a) for a in payload["attachment_ids"]]))
            .order_by(Attachment.created_at)
            .all()
        )
        for attachment in attachments:
            if attachment.ocr_status is None:
                scan(db, attachment, order.user_id)
        db.commit()

        # The seller is told once per message, with the reading of the first
        # image — a customer sending two screenshots sent one payment.
        if order.payment_status == PAID:
            return
        reading = ""
        if attachments:
            user = order.user
            assessment = assess(db, attachments[0], order)
            line = verdict_line(assessment, order, user.language if user else "en")
            reading = f"\n{line}" if line else ""
        alert_receipt(db, order, payload.get("customer", ""), reading=reading)
        db.commit()
    finally:
        db.close()
