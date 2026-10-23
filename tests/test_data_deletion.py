"""Closing an account, and erasing a customer at Meta's request."""

import base64
import hashlib
import hmac
import json
from unittest import mock

from sqlalchemy import inspect, text

from app.core.config import settings
from app.database import Base, engine
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.data_deletion_request import DataDeletionRequest
from app.models.message import Message
from app.models.order import Order
from app.models.user import User
from app.services import data_deletion
from tests import ai, webhooks as wh
from tests.conftest import PASSWORD
from tests.test_operations import supplier
from tests.test_product_images import PNG, upload
from tests.test_staff import add_staff


def busy_shop(client, seller):
    """A shop with one of everything — including the sales records that
    point at products with RESTRICT."""
    product = seller.product(stock=20)
    upload(client, seller, product["id"], [("front.png", PNG, "image/png")])
    customer = seller.customer()
    conversation = seller.conversation(customer["id"])
    with ai.replies("Hi"):
        client.post(f"/api/v1/chat/{conversation['id']}",
                    json={"message": "hello", "message_type": "text"}, headers=seller.headers)
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}],
                         conversation_id=conversation["id"]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "delivered"}, headers=seller.headers)
    returned = client.post("/api/v1/operations/returns", json={
        "order_id": order["id"], "reason": "Too big", "refund_amount": "5.00",
        "items": [{"order_item_id": order["items"][0]["id"], "quantity": 1, "restock_quantity": 1}],
    }, headers=seller.headers)
    assert returned.status_code == 201, returned.text
    po = client.post("/api/v1/operations/purchase-orders", json={
        "supplier_id": supplier(client, seller)["id"],
        "items": [{"variant_id": product["variants"][0]["id"], "ordered_quantity": 5, "unit_cost": "2.00"}],
    }, headers=seller.headers)
    assert po.status_code == 201, po.text
    wh.connect(client, seller, platform="telegram", external_id=f"bot_{seller.email[:6]}",
               token="bot-token")
    client.post("/api/v1/payment-qrs/", data={"name": "ABA"},
                files={"file": ("qr.png", PNG, "image/png")}, headers=seller.headers)
    add_staff(client, seller)


def rows_for(db, user_id) -> dict[str, int]:
    """Rows in every table that has a user_id column, for this user."""
    counts = {}
    for table in Base.metadata.sorted_tables:
        if "user_id" in table.c:
            n = db.execute(text(f"SELECT count(*) FROM {table.name} WHERE user_id = :u"),
                           {"u": user_id}).scalar()
            if n:
                counts[table.name] = n
    return counts


def delete(client, seller, password=PASSWORD):
    with mock.patch.object(data_deletion, "remove_telegram_webhook", return_value=True) as unhook:
        r = client.post("/api/v1/auth/delete-account", json={"password": password},
                        headers=seller.headers)
    return r, unhook


def test_closing_the_account_removes_the_whole_shop(client, seller, other_seller, db):
    busy_shop(client, seller)
    busy_shop(client, other_seller)
    owner_id = db.query(User).filter(User.email == seller.email).one().id
    other_id = db.query(User).filter(User.email == other_seller.email).one().id
    before_other = rows_for(db, other_id)
    assert rows_for(db, owner_id)

    r, unhook = delete(client, seller)

    assert r.status_code == 204, r.text
    unhook.assert_called_once()
    db.expire_all()
    assert rows_for(db, owner_id) == {}
    assert db.query(User).filter((User.id == owner_id) | (User.shop_id == owner_id)).count() == 0
    assert rows_for(db, other_id) == before_other, "another shop is untouched"
    assert client.get("/api/v1/auth/me", headers=seller.headers).status_code == 401


def test_closing_needs_the_password(client, seller, db):
    r, _ = delete(client, seller, password="not-it")
    assert r.status_code == 403
    assert db.query(User).filter(User.email == seller.email).count() == 1


def test_staff_cannot_close_the_shop(client, seller, db):
    staff = add_staff(client, seller)
    r, _ = delete(client, staff)
    assert r.status_code == 403
    assert db.query(User).count() == 2


def test_every_table_with_a_user_is_covered():
    """A new table with a user_id must either cascade from users or be
    cleared by delete_shop — this lists the ones that do neither."""
    inspector = inspect(engine)
    blocking = []
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "users" and fk["options"].get("ondelete") not in (
                    "CASCADE", "SET NULL"):
                blocking.append(table)
    assert blocking == []


# ── Meta's data deletion callback ────────────────────────────────────────────

def signed(payload: dict, secret: str | None = None) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    key = (secret or settings.META_APP_SECRET).encode()
    sig = base64.urlsafe_b64encode(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{sig.rstrip(b'=').decode()}.{body}"


def test_a_signed_request_erases_that_person_from_every_shop(client, seller, other_seller, db):
    # The same person has written to two shops, and ordered from both.
    for n, shop in enumerate((seller, other_seller)):
        wh.connect(client, shop, external_id=f"page-{n}")
        with wh.sends(), ai.replies("Hi"):
            wh.post_messenger(client, wh.messenger_payload(f"page-{n}", "psid-42", "hi", mid=f"m{n}"))
        conversation = client.get("/api/v1/conversations/", headers=shop.headers).json()[0]
        product = shop.product(stock=5)
        r = shop.order(conversation["customer_id"], [{"product_id": product["id"], "quantity": 1}],
                       conversation_id=conversation["id"], delivery_address="St 271, Phnom Penh")
        assert r.status_code == 201, r.text

    r = client.post("/api/v1/privacy/meta-data-deletion",
                    data={"signed_request": signed({"algorithm": "HMAC-SHA256", "user_id": "psid-42"})})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].endswith(f"/data-deletion?code={body['confirmation_code']}")
    db.expire_all()
    assert db.query(Message).count() == 0
    assert db.query(Conversation).count() == 0
    assert {c.name for c in db.query(Customer).all()} == {data_deletion.ANONYMOUS}
    orders = db.query(Order).all()
    assert len(orders) == 2, "the shops keep their sales"
    assert all(o.delivery_address is None for o in orders)
    record = db.query(DataDeletionRequest).one()
    assert record.customers_erased == 2
    assert "psid-42" not in record.subject_hash

    status = client.get(f"/api/v1/privacy/deletion-requests/{body['confirmation_code'].lower()}")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_an_unsigned_or_forged_request_is_refused(client, db):
    forged = signed({"algorithm": "HMAC-SHA256", "user_id": "psid-42"}, secret="not-the-secret")
    for value in (forged, "garbage", "a.b"):
        r = client.post("/api/v1/privacy/meta-data-deletion", data={"signed_request": value})
        assert r.status_code == 400
    assert db.query(DataDeletionRequest).count() == 0


def test_an_unknown_code_is_not_found(client):
    assert client.get("/api/v1/privacy/deletion-requests/NOPE").status_code == 404
