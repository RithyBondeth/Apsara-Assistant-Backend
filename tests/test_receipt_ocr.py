"""Reading customer receipts, and judging them against the order."""

import json
from unittest import mock

from openai import APIConnectionError

from app.core.config import settings
from app.models.attachment import Attachment
from app.services import receipts
from app.services.platforms import AttachmentDownload
from tests import ai, webhooks as wh
from tests.test_receipts import order_with_receipt
from tests.test_seller_alerts import alerts, customer_says, link, the_conversation


def reading(**fields):
    """What the model would return for a receipt, as the JSON it sends back."""
    base = {"is_receipt": True, "amount": 8.0, "currency": "USD", "reference": "TRX-001",
            "bank": "ABA", "payer": "Srey Neang", "payee": "Sok Silk Shop",
            "paid_at": "22 Sep 2026 10:15"}
    base.update(fields)
    return json.dumps(base)


def order_on_channel(client, seller, db, integration, *, chat_id=555, price="4.00", qty=2):
    """A customer who has talked to the bot, and an unpaid order for them."""
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "I want some", chat_id=chat_id, update_id=chat_id)
    customer = next(c for c in client.get("/api/v1/customers/", headers=seller.headers).json()
                    if c["platform_id"] == str(chat_id))
    conversation = next(c for c in client.get("/api/v1/conversations/", headers=seller.headers).json()
                        if c["customer_id"] == customer["id"])
    product = seller.product(name=f"Krama {chat_id}", price=price, stock=10)
    r = seller.order(conversation["customer_id"], [{"product_id": product["id"], "quantity": qty}],
                     conversation_id=conversation["id"])
    assert r.status_code == 201, r.text
    return r.json(), conversation


def send_photo(client, integration, *, chat_id=555, update_id=900):
    photo = AttachmentDownload(b"receipt-bytes", "image/jpeg", "receipt.jpg")
    update = wh.telegram_update(chat_id, "", update_id=update_id)
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "big", "file_size": 5}]
    with mock.patch("app.services.inbound.download_attachment", return_value=photo):
        return wh.post_telegram(client, integration["id"], update,
                                secret=integration["webhook_secret"])


def receipts_of(client, seller, order):
    return client.get(f"/api/v1/orders/{order['id']}/receipts", headers=seller.headers).json()


# ── Reading and matching ─────────────────────────────────────────────────────

def test_a_matching_receipt_is_read_and_the_alert_says_so(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.replies(reading()) as captured:
        send_photo(client, integration)

    # The image went to the vision model as a data URI, with strict output.
    assert captured["model"] == settings.OPENAI_VISION_MODEL
    image = captured["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert captured["response_format"]["json_schema"]["strict"] is True

    [alert] = to_seller
    assert "✅ Receipt reads 8.00 USD — matches" in alert["text"]

    [receipt] = receipts_of(client, seller, order)
    assert receipt["verdict"] == "match"
    assert receipt["read"] == "8.00 USD"
    assert receipt["ocr_amount"] == "8.00"
    assert receipt["ocr_reference"] == "TRX-001"
    assert receipt["ocr_data"]["bank"] == "ABA"


def test_an_amount_that_does_not_match_is_called_out(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.replies(reading(amount=5)):
        send_photo(client, integration)

    assert "⚠️ Receipt reads 5.00 USD, the order is 8.00 USD" in to_seller[0]["text"]
    assert receipts_of(client, seller, order)[0]["verdict"] == "amount_mismatch"


def test_a_riel_receipt_on_a_dollar_order_is_compared_at_the_shops_rate(client, seller, db):
    """32,000 riel for 8 dollars is what a customer paying at their bank's
    4,000 rate sends; at the shop's 4,100 that is 7.80, within tolerance."""
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.replies(reading(amount=32000, currency="KHR")):
        send_photo(client, integration)

    assert "✅ Receipt reads 32,000 KHR ≈ 7.80 USD — matches" in to_seller[0]["text"]
    [receipt] = receipts_of(client, seller, order)
    assert receipt["verdict"] == "match"
    assert receipt["read"] == "32,000 KHR ≈ 7.80 USD"

    # Confirming records what was actually paid, not what was priced.
    client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt['id']}/confirm",
                headers=seller.headers)
    paid = client.get(f"/api/v1/orders/{order['id']}", headers=seller.headers).json()
    assert (paid["paid_amount"], paid["paid_currency"]) == ("32000.00", "KHR")
    assert (paid["total_amount"], paid["currency"]) == ("8.00", "USD")


def test_a_riel_receipt_far_off_the_rate_is_a_mismatch(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.replies(reading(amount=20000, currency="KHR")):
        send_photo(client, integration)

    assert "⚠️ Receipt reads 20,000 KHR ≈ 4.88 USD, the order is 8.00 USD" in to_seller[0]["text"]
    assert receipts_of(client, seller, order)[0]["verdict"] == "amount_mismatch"


def test_the_same_reference_offered_twice_is_a_duplicate(client, seller, db):
    """One transfer, two orders — the fraud a small shop actually meets."""
    integration = link(client, seller, db)
    first, _ = order_on_channel(client, seller, db, integration, chat_id=555)
    with wh.sends(), alerts(), ai.replies(reading()):
        send_photo(client, integration, chat_id=555, update_id=901)
    [receipt] = receipts_of(client, seller, first)
    assert receipt["verdict"] == "match"
    client.post(f"/api/v1/orders/{first['id']}/receipts/{receipt['id']}/confirm",
                headers=seller.headers)

    second, _ = order_on_channel(client, seller, db, integration, chat_id=556)
    with wh.sends(), alerts() as to_seller, ai.replies(reading()):
        send_photo(client, integration, chat_id=556, update_id=902)

    assert "🚫 Same reference" in to_seller[0]["text"]
    [again] = receipts_of(client, seller, second)
    assert again["verdict"] == "duplicate"
    assert again["duplicate_of_order_id"] == first["id"]
    # The earlier receipt is untouched by the later one.
    assert receipts_of(client, seller, first)[0]["verdict"] == "match"


def test_a_photo_that_is_not_a_receipt_says_so(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, \
            ai.replies(reading(is_receipt=False, amount=None, currency=None, reference=None)):
        send_photo(client, integration)

    assert "does not look like a payment receipt" in to_seller[0]["text"]
    [receipt] = receipts_of(client, seller, order)
    assert receipt["verdict"] == "not_a_receipt"
    assert receipt["ocr_status"] == "not_receipt"


def test_a_model_outage_still_alerts_and_is_retryable(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.fails(APIConnectionError(request=mock.Mock())):
        send_photo(client, integration)

    # The seller still hears about the receipt; they just have to look.
    assert "Could not read the receipt" in to_seller[0]["text"]
    [receipt] = receipts_of(client, seller, order)
    assert receipt["verdict"] == "unreadable"
    assert receipt["ocr_status"] == "failed"

    # And can ask again once the model is back.
    with ai.replies(reading()):
        r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt['id']}/scan",
                        headers=seller.headers)
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "match"


def test_verdicts_follow_the_sellers_language(client, seller, db):
    integration = link(client, seller, db, language="km")
    order_on_channel(client, seller, db, integration)

    with wh.sends(), alerts() as to_seller, ai.replies(reading()):
        send_photo(client, integration)

    assert "ត្រូវគ្នា" in to_seller[0]["text"]


# ── On demand ────────────────────────────────────────────────────────────────

def test_a_receipt_from_before_scanning_can_be_scanned_now(client, seller, other_seller, db):
    order, receipt, _, _ = order_with_receipt(seller, db)

    [listed] = receipts_of(client, seller, order)
    assert listed["verdict"] is None and listed["ocr_status"] is None

    with ai.replies(reading(amount=12.5)):
        r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/scan",
                        headers=seller.headers)
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "match"

    # Another seller cannot scan it, or even learn it exists.
    with ai.replies(reading()):
        r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/scan",
                        headers=other_seller.headers)
    assert r.status_code == 404


def test_receipt_scans_have_their_own_daily_budget(client, seller, db, monkeypatch):
    monkeypatch.setattr(settings, "AI_DAILY_RECEIPT_LIMIT", 1)
    order, receipt, _, _ = order_with_receipt(seller, db)

    with ai.replies(reading(amount=12.5)):
        first = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/scan",
                            headers=seller.headers).json()
        second = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/scan",
                             headers=seller.headers).json()

    assert first["verdict"] == "match"
    assert second["verdict"] == "unreadable"
    assert "limit" in second["ocr_data"]["error"]
    # Replies are a separate allowance and untouched.
    from app.services.quota import used_today
    from app.models.user import User
    user = db.query(User).filter(User.email == seller.email).one()
    assert used_today(db, user.id) == 0


def test_scanning_is_off_when_the_limit_is_zero(client, seller, db, monkeypatch):
    monkeypatch.setattr(settings, "AI_DAILY_RECEIPT_LIMIT", 0)
    order, receipt, _, _ = order_with_receipt(seller, db)
    with ai.replies(reading()) as captured:
        r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt.id}/scan",
                        headers=seller.headers)
    assert r.json()["ocr_status"] == "failed"
    assert captured == {}, "no model call when scanning is disabled"


# ── Pure pieces ──────────────────────────────────────────────────────────────

def test_readings_are_normalised_before_they_are_stored():
    attachment = Attachment()
    receipts.apply_reading(attachment, receipts.ReceiptReading(
        is_receipt=True, amount=12.345, currency=" usd ", reference="  ABC123 ",
        bank=None, payer=None, payee=None, paid_at=None,
    ))
    assert str(attachment.ocr_amount) == "12.35"
    assert attachment.ocr_currency == "USD"
    assert attachment.ocr_reference == "ABC123"

    receipts.apply_reading(attachment, receipts.ReceiptReading(
        is_receipt=True, amount=1, currency="EUR", reference=None,
        bank=None, payer=None, payee=None, paid_at=None,
    ))
    # A currency the shop cannot price in is treated as unstated, not wrong.
    assert attachment.ocr_currency is None
