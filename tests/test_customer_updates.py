"""Messages to the customer when their order moves on."""

from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from app.core.clock import utcnow
from app.models.conversation import Conversation
from app.models.job import Job, PENDING
from app.models.message import Message
from app.models.order import Order
from app.models.user import User
from app.services import customer_updates, queue
from tests import ai, webhooks as wh
from tests.test_receipt_ocr import order_on_channel, reading, send_photo
from tests.test_seller_alerts import alerts, customer_says, link


@contextmanager
def to_customer(ok=True):
    sent = []

    def _send(platform, token, recipient_id, text, *, tag=None):
        sent.append({"platform": platform, "recipient_id": recipient_id,
                     "text": text, "tag": tag})
        return ok

    with mock.patch.object(customer_updates, "send_reply", _send):
        yield sent


def set_status(client, seller, order, status, **extra):
    r = client.patch(f"/api/v1/orders/{order['id']}", json={"status": status, **extra},
                     headers=seller.headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_confirming_tells_the_customer_with_the_total(client, seller, db):
    integration = link(client, seller, db)
    order, conversation = order_on_channel(client, seller, db, integration)

    with to_customer() as sent:
        set_status(client, seller, order, "confirmed")

    [message] = sent
    assert message["platform"] == "telegram"
    assert message["recipient_id"] == "555"
    assert message["text"] == (f"✅ Your order #{order['id'][:8].upper()} is confirmed. "
                               "Total: 8.00 USD (32,800 KHR). We'll let you know when it's on its way.")
    thread = client.get(f"/api/v1/conversations/{conversation['id']}/messages",
                        headers=seller.headers).json()
    assert thread[-1]["content"] == message["text"]
    assert thread[-1]["sender_type"] == "assistant"
    db.expire_all()
    assert db.query(Order).one().customer_notified_events == ["confirmed"]


def test_a_customer_writing_khmer_is_answered_in_khmer(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "ខ្ញុំចង់ទិញ", update_id=1)
    order, _ = order_on_channel(client, seller, db, integration)

    with to_customer() as sent:
        set_status(client, seller, order, "shipped")

    assert sent[0]["text"].startswith("🚚 ការបញ្ជាទិញ #")


def test_bookkeeping_statuses_send_nothing(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with to_customer() as sent:
        set_status(client, seller, order, "processing")
        set_status(client, seller, order, "pending")

    assert sent == []


def test_each_event_is_announced_once(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with to_customer() as sent:
        set_status(client, seller, order, "confirmed")
        set_status(client, seller, order, "pending")
        set_status(client, seller, order, "confirmed")
        set_status(client, seller, order, "delivered")

    assert [m["text"][0] for m in sent] == ["✅", "📦"]


def test_the_seller_can_change_a_status_quietly(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)

    with to_customer() as sent:
        set_status(client, seller, order, "cancelled", notify_customer=False)

    assert sent == []


def test_the_shop_can_turn_updates_off(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)
    r = client.patch("/api/v1/auth/me", json={"customer_updates_enabled": False},
                     headers=seller.headers)
    assert r.status_code == 200 and r.json()["customer_updates_enabled"] is False

    with to_customer() as sent:
        set_status(client, seller, order, "confirmed")

    assert sent == []


def test_a_rehearsal_order_messages_nobody(client, seller):
    conversation = seller.conversation()
    product = seller.product(stock=5)
    order = seller.order(conversation["customer_id"],
                         [{"product_id": product["id"], "quantity": 1}],
                         conversation_id=conversation["id"]).json()

    with to_customer() as sent:
        set_status(client, seller, order, "confirmed")

    assert sent == []


def test_a_confirmed_receipt_says_payment_received(client, seller, db):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)
    with wh.sends(), alerts(), ai.replies(reading()):
        send_photo(client, integration)
    receipt = client.get(f"/api/v1/orders/{order['id']}/receipts", headers=seller.headers).json()[0]

    with to_customer() as sent:
        r = client.post(f"/api/v1/orders/{order['id']}/receipts/{receipt['id']}/confirm",
                        headers=seller.headers)
    assert r.status_code == 200, r.text

    assert sent[0]["text"].startswith("💵 Payment received for order #")


def test_messenger_past_the_24_hour_window_uses_the_update_tag(client, seller, db):
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))
    conversation = db.query(Conversation).one()
    product = seller.product(stock=5)
    order = seller.order(str(conversation.customer_id),
                         [{"product_id": product["id"], "quantity": 1}],
                         conversation_id=str(conversation.id)).json()

    with to_customer() as sent:
        set_status(client, seller, order, "confirmed")
    conversation.last_customer_message_at = utcnow() - timedelta(hours=30)
    db.commit()
    with to_customer() as later:
        set_status(client, seller, order, "shipped")

    assert sent[0]["tag"] is None
    assert later[0]["tag"] == customer_updates.MESSENGER_TAG


def test_a_refused_update_is_retried_and_not_recorded(client, seller, db):
    integration = link(client, seller, db)
    order, conversation = order_on_channel(client, seller, db, integration)
    before = db.query(Message).count()

    with to_customer(ok=False):
        set_status(client, seller, order, "confirmed")

    db.expire_all()
    job = db.query(Job).filter(Job.kind == customer_updates.CUSTOMER_UPDATE).one()
    assert job.status == PENDING and "refused" in job.last_error
    assert db.query(Message).count() == before
    assert db.query(Order).one().customer_notified_events == []


def test_a_status_reverted_before_sending_is_not_announced(client, seller, db, monkeypatch):
    integration = link(client, seller, db)
    order, _ = order_on_channel(client, seller, db, integration)
    monkeypatch.setattr(queue.settings, "JOB_RUNNER", "worker")

    set_status(client, seller, order, "confirmed")
    set_status(client, seller, order, "pending")
    with to_customer() as sent:
        queue.drain()

    assert sent == []


def test_the_seller_reads_the_setting(client, seller, db):
    me = client.get("/api/v1/auth/me", headers=seller.headers).json()
    assert me["customer_updates_enabled"] is True
    assert db.query(User).one().customer_updates_enabled is True
