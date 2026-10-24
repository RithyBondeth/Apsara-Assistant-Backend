"""A stand-in for the Claude API, for clicking through the app locally.

Turned on with AI_FAKE=true, and only ever in development (see
Settings.check_ai). It answers the same requests ai_service sends, from what
those requests already contain — the catalogue in the system prompt, the
customer's last message — so replies, order drafts and receipt readings all
flow through the real code after the model call. Nothing here is clever: the
point is to exercise the app, not to imitate the model.
"""

from __future__ import annotations

import hashlib
import json
import re
import types

from app.services.ai_service import NEEDS_SELLER_MARKER, PAYMENT_QR_MARKER, PHOTO_MARKER

# Every reply starts with this, so a demo answer can never pass for a real one.
LABEL = "[Demo AI — no model was called]"

# The amount every demo receipt reads. Price an order at this to see a match.
RECEIPT_AMOUNT = 8.0
RECEIPT_CURRENCY = "USD"

KHMER = re.compile(r"[ក-៿]")
CATALOGUE_LINE = re.compile(r"^• (.+)$", re.MULTILINE)
DRAFT_ROW = re.compile(
    r"^- product=(?P<product>[0-9a-f-]{36})"
    r"(?: \| variant=(?P<variant>[0-9a-f-]{36}))? \| (?P<name>[^|\n]+?)(?: \||$)",
    re.MULTILINE,
)

WANTS_QR = ("pay", "qr", "aba", "bank", "transfer", "bong luy", "ទូទាត់", "បង់")
WANTS_PHOTO = ("photo", "picture", "pic", "image", "rup", "រូប")
WANTS_SELLER = ("discount", "seller", "human", "complain", "refund", "bunjos", "បញ្ចុះ")


class FakeClient:
    """Has the one method ai_service calls: `client.beta.messages.create`."""

    def __init__(self) -> None:
        self.beta = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=self.create))

    def create(self, **request) -> types.SimpleNamespace:
        schema = ((request.get("output_config") or {}).get("format") or {}).get("schema") or {}
        title = schema.get("title")
        if title == "ExtractedOrderDraft":
            text = json.dumps(order_draft(request))
        elif title == "ReceiptReading":
            text = json.dumps(receipt_reading(request))
        else:
            text = reply(request)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            stop_details=None,
        )


def _last_customer_text(request: dict) -> str:
    for turn in reversed(request.get("messages") or []):
        if turn["role"] == "user" and isinstance(turn["content"], str):
            return turn["content"]
    return ""


def reply(request: dict) -> str:
    system = request.get("system") or ""
    said = _last_customer_text(request)
    lowered = said.lower()

    products = [line.strip() for line in CATALOGUE_LINE.findall(system)]
    named = [line for line in products if line.split(" — ")[0].lower() in lowered]
    if named:
        body = f"{named[0]}."
    elif products:
        body = f"We have {len(products)} product(s). For example: {products[0]}."
    else:
        body = "This shop has no products listed yet."
    if KHMER.search(said):
        body = f"សួស្តី! {body}"

    markers: list[str] = []
    if "PAYMENT QR" in system and any(word in lowered for word in WANTS_QR):
        body += " Here is the payment QR — please send the receipt after paying."
        markers.append(PAYMENT_QR_MARKER)
    photo = PHOTO_MARKER.search(system)
    if photo and any(word in lowered for word in WANTS_PHOTO):
        body += " Here is a photo."
        markers.append(photo.group(0))
    if any(word in lowered for word in WANTS_SELLER):
        body += " Let me check with the seller."
        markers.append(NEEDS_SELLER_MARKER)

    return "\n".join([f"{LABEL} {body}", *markers])


def order_draft(request: dict) -> dict:
    """One of each catalogue item the transcript names, else the first item."""
    system = request.get("system") or ""
    # Only what the customer said: the shop's turns name products too.
    transcript = "\n".join(
        line for line in _last_customer_text(request).lower().splitlines()
        if line.startswith("customer:")
    )
    rows = list(DRAFT_ROW.finditer(system))
    named = [row for row in rows if row["name"].strip().lower() in transcript]
    chosen = named or rows[:1]
    return {
        "items": [
            {"product_id": row["product"], "variant_id": row["variant"], "quantity": 1}
            for row in chosen
        ],
        "delivery_address": None,
        "notes": "Demo draft — no model was called.",
    }


def receipt_reading(request: dict) -> dict:
    """The same amount for every image; the reference follows the image, so
    sending one picture twice still shows as a duplicate."""
    image = next(
        (block for turn in request.get("messages") or []
         if isinstance(turn["content"], list)
         for block in turn["content"] if block.get("type") == "image"),
        None,
    )
    digest = hashlib.md5((image or {}).get("source", {}).get("data", "").encode()).hexdigest()
    return {
        "is_receipt": True,
        "amount": RECEIPT_AMOUNT,
        "currency": RECEIPT_CURRENCY,
        "reference": f"DEMO-{digest[:8].upper()}",
        "bank": "Demo Bank",
        "payer": "Demo Customer",
        "payee": None,
        "paid_at": None,
    }
