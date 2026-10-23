"""Dynamic KHQR for an order, and Bakong confirming it was paid."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest

from app.core.clock import utcnow
from app.models.job import Job
from app.models.order import PAID, Order
from app.models.user import User
from app.services import khqr, khqr_orders, seller_alerts
from tests import ai, webhooks as wh
from tests.test_receipt_ocr import order_on_channel
from tests.test_seller_alerts import alerts, customer_says, link

ACCOUNT = "sokdara@aclb"


def enable(client, seller, **extra):
    r = client.patch("/api/v1/auth/me", json={
        "khqr_enabled": True, "bakong_account_id": ACCOUNT,
        "khqr_merchant_name": "Sok Silk Shop", "khqr_merchant_city": "Phnom Penh",
        "bakong_api_token": "bakong-token", **extra}, headers=seller.headers)
    assert r.status_code == 200, r.text
    return r.json()


def plain_order(seller, price="8.00"):
    product = seller.product(price=price, stock=5)
    customer = seller.customer()
    r = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])
    assert r.status_code == 201, r.text
    return r.json()


def bakong(body, status=200):
    response = mock.MagicMock(status_code=status, text=str(body))
    response.json.return_value = body
    return mock.patch.object(khqr.httpx, "post", return_value=response)


def found(amount=8.0, currency="USD"):
    return bakong({"responseCode": 0, "data": {"hash": "abc123", "amount": amount,
                                               "currency": currency}})


NOT_FOUND = {"responseCode": 1, "errorCode": 1, "responseMessage": "not found"}


# ── The code itself ──────────────────────────────────────────────────────────

def test_crc_is_ccitt_false():
    assert khqr.crc16("123456789") == "29B1"


def test_a_dollar_code_carries_the_amount_and_a_valid_checksum():
    payload = khqr.build(account_id=ACCOUNT, merchant_name="Sok Silk Shop",
                         merchant_city="Phnom Penh", amount=Decimal("8.50"),
                         currency="USD", bill_number="ORD-1A2B3C4D",
                         expires_at_ms=1793000000000, created_at_ms=1792999000000)
    fields = khqr.parse(payload)

    assert fields["00"] == "01"
    assert fields["01"] == "12"
    assert khqr.parse(fields["29"]) == {"00": ACCOUNT}
    assert fields["53"] == "840"
    assert fields["54"] == "8.5"
    assert fields["58"] == "KH"
    assert fields["59"] == "Sok Silk Shop"
    assert fields["62"] == "0112ORD-1A2B3C4D"
    assert khqr.parse(fields["99"]) == {"00": "1792999000000", "01": "1793000000000"}
    assert fields["63"] == khqr.crc16(payload[:-4])


def test_riel_is_whole_and_a_merchant_uses_template_30():
    payload = khqr.build(account_id=ACCOUNT, merchant_name="Shop", merchant_city="Siem Reap",
                         amount=Decimal("32800"), currency="KHR", bill_number="X",
                         merchant_id="M123", acquiring_bank="ACLEDA")
    fields = khqr.parse(payload)
    assert fields["53"] == "116" and fields["54"] == "32800"
    assert khqr.parse(fields["30"]) == {"00": ACCOUNT, "01": "M123", "02": "ACLEDA"}
    assert "29" not in fields


@pytest.mark.parametrize("kw, message", [
    ({"account_id": "nobank"}, "name@bank"),
    ({"amount": Decimal("0")}, "amount"),
    ({"currency": "EUR"}, "USD and KHR"),
    ({"merchant_name": "ហាង"}, "Latin"),
])
def test_codes_that_cannot_be_paid_are_refused(kw, message):
    args = dict(account_id=ACCOUNT, merchant_name="Shop", merchant_city="PP",
                amount=Decimal("1"), currency="USD", bill_number="X")
    args.update(kw)
    with pytest.raises(khqr.KhqrError, match=message):
        khqr.build(**args)


# ── Settings ─────────────────────────────────────────────────────────────────

def test_the_bakong_token_is_stored_encrypted_and_never_returned(client, seller, db):
    me = enable(client, seller)
    assert me["bakong_token_set"] is True
    assert "bakong_api_token" not in me
    stored = db.query(User).one().bakong_api_token
    assert stored and "bakong-token" not in stored
    assert khqr.api_token(db.query(User).one()) == "bakong-token"

    cleared = client.patch("/api/v1/auth/me", json={"bakong_api_token": ""},
                           headers=seller.headers).json()
    assert cleared["bakong_token_set"] is False


@pytest.mark.parametrize("field, value", [
    ("bakong_account_id", "no-at-sign"),
    ("khqr_merchant_name", "ហាងសូត្រ"),
])
def test_unusable_shop_details_are_rejected(client, seller, field, value):
    r = client.patch("/api/v1/auth/me", json={field: value}, headers=seller.headers)
    assert r.status_code == 422


# ── Issuing ──────────────────────────────────────────────────────────────────

def test_a_shop_without_khqr_cannot_issue_one(client, seller):
    order = plain_order(seller)
    r = client.post(f"/api/v1/orders/{order['id']}/khqr", headers=seller.headers)
    assert r.status_code == 400


def test_issuing_gives_the_order_a_code_and_schedules_a_check(client, seller, db):
    enable(client, seller)
    order = plain_order(seller)

    body = client.post(f"/api/v1/orders/{order['id']}/khqr", headers=seller.headers).json()

    row = db.query(Order).one()
    assert body["khqr_status"] == "issued"
    assert body["khqr_image_url"].endswith(f"/api/v1/media/khqr/{row.id}/{row.khqr_md5}.png")
    assert khqr.parse(row.khqr_payload)["54"] == "8"
    job = db.query(Job).filter(Job.kind == khqr_orders.KHQR_CHECK).one()
    assert job.run_after > utcnow()

    again = client.post(f"/api/v1/orders/{order['id']}/khqr", headers=seller.headers).json()
    assert again["khqr_image_url"] == body["khqr_image_url"], "still payable, so reused"


def test_the_image_is_served_only_at_the_current_code(client, seller, db):
    enable(client, seller)
    order = plain_order(seller)
    body = client.post(f"/api/v1/orders/{order['id']}/khqr", headers=seller.headers).json()

    served = client.get(body["khqr_image_url"])
    assert served.status_code == 200
    assert served.content.startswith(b"\x89PNG")
    wrong = body["khqr_image_url"].rsplit("/", 1)[0] + "/" + "0" * 32 + ".png"
    assert client.get(wrong).status_code == 404


# ── Watching Bakong ──────────────────────────────────────────────────────────

def issued(client, seller, db, price="8.00"):
    enable(client, seller)
    order = plain_order(seller, price=price)
    client.post(f"/api/v1/orders/{order['id']}/khqr", headers=seller.headers)
    row = db.query(Order).one()
    return {"order_id": str(row.id), "md5": row.khqr_md5}


def test_a_payment_bakong_has_seen_marks_the_order_paid(client, seller, db):
    link(client, seller, db)  # so the seller has somewhere to be told
    payload = issued(client, seller, db)

    with found(), mock.patch.object(seller_alerts, "send_reply", return_value=True):
        khqr_orders.run_check(payload)

    db.expire_all()
    order = db.query(Order).one()
    assert order.payment_status == PAID
    assert order.payment_method == "khqr"
    assert order.paid_amount == Decimal("8.00") and order.paid_currency == "USD"
    assert order.khqr_status == "paid"
    assert order.reservation_expires_at is None
    kinds = {job.kind for job in db.query(Job).all()}
    assert "seller_alert" in kinds


def test_no_payment_yet_looks_again_later(client, seller, db):
    payload = issued(client, seller, db)
    with bakong(NOT_FOUND) as call:
        khqr_orders.run_check(payload)

    assert call.call_args.kwargs["json"] == {"md5": payload["md5"]}
    assert call.call_args.kwargs["headers"] == {"Authorization": "Bearer bakong-token"}
    db.expire_all()
    assert db.query(Order).one().payment_status != PAID
    assert db.query(Job).filter(Job.kind == khqr_orders.KHQR_CHECK).count() == 2


def test_a_different_amount_is_flagged_not_accepted(client, seller, db):
    payload = issued(client, seller, db)
    with found(amount=5.0):
        khqr_orders.run_check(payload)

    db.expire_all()
    order = db.query(Order).one()
    assert order.payment_status != PAID
    assert order.khqr_status == "mismatch"


def test_an_expired_code_stops_being_watched(client, seller, db):
    payload = issued(client, seller, db)
    db.query(Order).update({Order.khqr_expires_at: utcnow() - timedelta(minutes=1)})
    db.commit()

    with bakong(NOT_FOUND) as call:
        khqr_orders.run_check(payload)

    call.assert_not_called()
    db.expire_all()
    assert db.query(Order).one().khqr_status == "expired"


def test_a_superseded_code_is_not_checked(client, seller, db):
    payload = issued(client, seller, db)
    with bakong(NOT_FOUND) as call:
        khqr_orders.run_check({**payload, "md5": "f" * 32})
    call.assert_not_called()


def test_checking_now_when_bakong_is_down_is_a_503(client, seller, db):
    payload = issued(client, seller, db)
    with bakong({"message": "nope"}, status=502):
        r = client.post(f"/api/v1/orders/{payload['order_id']}/khqr/check", headers=seller.headers)
    assert r.status_code == 503


def test_checking_now_can_find_the_payment(client, seller, db):
    payload = issued(client, seller, db)
    with found(), mock.patch.object(seller_alerts, "send_reply", return_value=True):
        r = client.post(f"/api/v1/orders/{payload['order_id']}/khqr/check", headers=seller.headers)
    assert r.status_code == 200
    assert r.json()["payment_status"] == "paid"


# ── The assistant ────────────────────────────────────────────────────────────

def test_the_assistant_sends_a_dynamic_code_for_the_open_order(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)
    enable(client, seller)

    with wh.sends(), wh.sends_images() as images, alerts(), \
            ai.replies("Please scan to pay.\n[SEND_PAYMENT_QR]") as captured:
        customer_says(client, integration, "how do I pay?", update_id=77)

    assert "PAYMENT QR" in captured.system_prompt
    db.expire_all()
    row = db.query(Order).filter(Order.id == order["id"]).one()
    assert row.khqr_status == "issued"
    assert [i["image_url"] for i in images] == [khqr_orders.image_url(row)]


def test_without_an_order_the_static_qr_is_still_sent(client, seller, db):
    integration = link(client, seller, db, payment_qr_url="https://example.com/qr.png")
    enable(client, seller)

    with wh.sends(), wh.sends_images() as images, alerts(), \
            ai.replies("Scan this.\n[SEND_PAYMENT_QR]"):
        customer_says(client, integration, "QR please")

    assert [i["image_url"] for i in images] == ["https://example.com/qr.png"]
