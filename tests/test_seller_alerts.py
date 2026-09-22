"""Waking the seller: Telegram alerts, and linking the chat they go to."""

from contextlib import contextmanager
from unittest import mock

from openai import APIConnectionError

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.user import User
from app.services import seller_alerts
from app.services.ai_service import NEEDS_SELLER_MARKER
from app.services.platforms import AttachmentDownload
from tests import ai, webhooks as wh
from tests.test_stripe import accept_signature, connect_stripe, fake_session, post_event, stripe_event  # noqa: F401

SELLER_CHAT = "777000"


@contextmanager
def alerts():
    """Capture everything the bot would say to the seller (not to customers)."""
    sent = []

    def _send(platform, token, recipient_id, text):
        sent.append({"platform": platform, "recipient_id": recipient_id, "text": text})
        return True

    with mock.patch.object(seller_alerts, "send_reply", _send), \
            mock.patch("app.services.telegram_link.send_reply", _send):
        yield sent


def link(client, seller, db, chat_id=SELLER_CHAT, **fields):
    """A seller with a Telegram bot and an alert chat already recorded."""
    integration = wh.connect(client, seller, platform="telegram",
                             external_id="sok_bot", token="bot-token")
    user = db.query(User).filter(User.email == seller.email).one()
    user.telegram_chat_id = chat_id
    for field, value in fields.items():
        setattr(user, field, value)
    db.commit()
    return integration


def customer_says(client, integration, text, chat_id=555, update_id=1, **extra):
    return wh.post_telegram(client, integration["id"],
                            wh.telegram_update(chat_id, text, update_id=update_id, **extra),
                            secret=integration["webhook_secret"])


def the_conversation(client, seller):
    convs = client.get("/api/v1/conversations/", headers=seller.headers).json()
    assert len(convs) == 1, convs
    return convs[0]


# ── Linking ──────────────────────────────────────────────────────────────────

def test_opening_the_link_records_the_chat_without_a_chat_id_in_sight(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="typed-by-hand")

    with mock.patch("app.services.telegram_link.telegram_bot_username", return_value="sok_bot"):
        r = client.post("/api/v1/auth/telegram-link", headers=seller.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bot_username"] == "sok_bot"
    assert body["link_url"] == f"https://t.me/sok_bot?start={body['code']}"

    # Telegram turns the opened link into "/start <code>" from the seller's chat.
    with alerts() as sent, ai.replies() as captured:
        r = customer_says(client, integration, f"/start {body['code']}",
                          chat_id=777000, last_name="Dara")
    assert r.status_code == 200

    me = client.get("/api/v1/auth/me", headers=seller.headers).json()
    assert me["telegram_chat_id"] == "777000"
    assert me["telegram_chat_name"] == "Srey Dara"
    assert me["telegram_linked_at"] is not None
    assert "Linked" in sent[0]["text"] and sent[0]["recipient_id"] == "777000"
    # The seller did not just become their own customer.
    assert db.query(Customer).count() == 0
    assert captured == {}


def test_a_link_only_works_once_and_a_stale_one_is_explained(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="sok_bot")
    with mock.patch("app.services.telegram_link.telegram_bot_username", return_value=None):
        code = client.post("/api/v1/auth/telegram-link", headers=seller.headers).json()["code"]

    with alerts() as sent:
        customer_says(client, integration, f"/start {code}", chat_id=1)
        customer_says(client, integration, f"/start {code}", chat_id=2, update_id=2)

    assert client.get("/api/v1/auth/me", headers=seller.headers).json()["telegram_chat_id"] == "1"
    assert "expired" in sent[1]["text"]
    assert db.query(Customer).count() == 0


def test_a_customers_plain_start_is_still_a_customer(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="sok_bot")
    with wh.sends(), alerts(), ai.replies("Sur sdei!"):
        customer_says(client, integration, "/start")
    assert db.query(Customer).count() == 1


def test_one_sellers_link_does_nothing_against_anothers_bot(client, seller, other_seller, db):
    rival_bot = wh.connect(client, other_seller, platform="telegram", external_id="rival_bot")
    wh.connect(client, seller, platform="telegram", external_id="sok_bot")
    with mock.patch("app.services.telegram_link.telegram_bot_username", return_value=None):
        code = client.post("/api/v1/auth/telegram-link", headers=seller.headers).json()["code"]

    with alerts():
        customer_says(client, rival_bot, f"/start {code}", chat_id=9)

    assert client.get("/api/v1/auth/me", headers=seller.headers).json()["telegram_chat_id"] is None
    assert client.get("/api/v1/auth/me", headers=other_seller.headers).json()["telegram_chat_id"] is None


def test_linking_needs_a_connected_bot(client, seller):
    r = client.post("/api/v1/auth/telegram-link", headers=seller.headers)
    assert r.status_code == 400
    assert "Telegram bot" in r.json()["detail"]


def test_unlinking_clears_the_chat(client, seller, db):
    link(client, seller, db)
    r = client.delete("/api/v1/auth/telegram-link", headers=seller.headers)
    assert r.status_code == 200
    assert r.json()["telegram_chat_id"] is None


# ── The assistant asking for a human ─────────────────────────────────────────

def test_escalation_alerts_the_seller_and_hides_the_marker(client, seller, db):
    integration = link(client, seller, db)

    with wh.sends() as to_customer, alerts() as to_seller, \
            ai.replies(f"Khnhom nung suor bong jam moung.\n{NEEDS_SELLER_MARKER}"):
        customer_says(client, integration, "Can I get a discount for 10?", last_name="Neang")

    assert to_customer[0]["text"] == "Khnhom nung suor bong jam moung."
    assert NEEDS_SELLER_MARKER not in to_customer[0]["text"]
    [alert] = to_seller
    assert alert["recipient_id"] == SELLER_CHAT
    assert "Srey Neang needs you" in alert["text"]
    assert "Can I get a discount for 10?" in alert["text"]
    conversation = the_conversation(client, seller)
    assert conversation["needs_attention_at"] is not None
    assert f"/chat?conversation={conversation['id']}" in alert["text"]


def test_one_alert_per_episode_until_the_seller_replies(client, seller, db):
    integration = link(client, seller, db)
    escalate = f"Jam moung.\n{NEEDS_SELLER_MARKER}"

    with wh.sends(), alerts() as to_seller, ai.replies(escalate):
        customer_says(client, integration, "discount?", update_id=1)
        customer_says(client, integration, "hello??", update_id=2)
    assert len(to_seller) == 1, "a waiting customer is one alert, not one per message"

    conversation = the_conversation(client, seller)
    with mock.patch("app.api.v1.endpoints.conversations.send_reply", return_value=True):
        r = client.post(f"/api/v1/conversations/{conversation['id']}/messages",
                        json={"content": "Yes, 10% off for 10."}, headers=seller.headers)
    assert r.status_code == 201, r.text
    assert the_conversation(client, seller)["needs_attention_at"] is None

    # The seller has answered; the thread was set to manual by that reply, so
    # the customer's next message is a new episode.
    with wh.sends(), alerts() as to_seller, ai.replies(escalate):
        customer_says(client, integration, "and delivery?", update_id=3)
    assert len(to_seller) == 1


def test_an_unanswered_customer_alerts_the_seller(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts() as to_seller, ai.fails(APIConnectionError(request=mock.Mock())):
        customer_says(client, integration, "tlai ponman?")
    assert "waiting for a reply" in to_seller[0]["text"]
    assert "tlai ponman?" in to_seller[0]["text"]


def test_a_manual_thread_alerts_on_each_new_episode(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hi")
    conversation = the_conversation(client, seller)
    client.patch(f"/api/v1/conversations/{conversation['id']}",
                 json={"handling_mode": "manual"}, headers=seller.headers)

    with wh.sends(), alerts() as to_seller, ai.replies() as captured:
        customer_says(client, integration, "are you there?", update_id=2)
    assert "New message from" in to_seller[0]["text"]
    assert captured == {}, "manual threads are never answered by the model"


def test_a_photo_with_nothing_to_pay_still_reaches_the_seller(client, seller, db):
    integration = link(client, seller, db)
    photo = AttachmentDownload(b"bytes", "image/jpeg", "photo.jpg")
    update = wh.telegram_update(555, "", update_id=1)
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "big", "file_size": 5}]

    with mock.patch("app.services.inbound.download_attachment", return_value=photo), \
            wh.sends(), alerts() as to_seller, ai.replies() as captured:
        wh.post_telegram(client, integration["id"], update, secret=integration["webhook_secret"])

    assert "sent a photo" in to_seller[0]["text"]
    assert captured == {}


# ── Payments ─────────────────────────────────────────────────────────────────

def test_a_receipt_for_an_unpaid_order_is_a_payment_alert(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "I want 2 krama")
    conversation = the_conversation(client, seller)
    product = seller.product(name="Krama", price="4.00", stock=10)
    r = seller.order(conversation["customer_id"],
                     [{"product_id": product["id"], "quantity": 2}],
                     conversation_id=conversation["id"])
    assert r.status_code == 201, r.text
    order = r.json()

    photo = AttachmentDownload(b"receipt", "image/jpeg", "receipt.jpg")
    update = wh.telegram_update(555, "", update_id=2)
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "big", "file_size": 5}]
    with mock.patch("app.services.inbound.download_attachment", return_value=photo), \
            wh.sends(), alerts() as to_seller:
        wh.post_telegram(client, integration["id"], update, secret=integration["webhook_secret"])

    [alert] = to_seller
    assert "payment receipt" in alert["text"]
    assert order["id"][:8].upper() in alert["text"]
    assert "8.00 USD" in alert["text"]
    assert f"/orders?order={order['id']}" in alert["text"]
    # A receipt is not a question: the thread is not flagged as unanswered.
    assert the_conversation(client, seller)["needs_attention_at"] is None


def test_a_card_payment_alerts_the_seller(client, seller, db, fake_session, accept_signature):  # noqa: F811
    link(client, seller, db)
    stripe = connect_stripe(client, seller)
    product = seller.product(price="20.00", stock=10)
    customer = seller.customer(name="Chan Sopheap")
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}]).json()
    accept_signature.event = stripe_event(order["id"])

    with alerts() as to_seller:
        post_event(client, stripe["id"], accept_signature.event)
        post_event(client, stripe["id"], accept_signature.event)

    assert len(to_seller) == 1, "a redelivered event is one payment, one alert"
    assert "paid" in to_seller[0]["text"] and "Chan Sopheap" in to_seller[0]["text"]
    assert "20.00 USD" in to_seller[0]["text"]


# ── Switches and language ────────────────────────────────────────────────────

def test_each_alert_kind_has_its_own_switch(client, seller, db):
    integration = link(client, seller, db, attention_telegram_enabled=False)
    with wh.sends(), alerts() as to_seller, ai.replies(f"Jam.\n{NEEDS_SELLER_MARKER}"):
        customer_says(client, integration, "discount?")
    assert to_seller == []
    # Still recorded for the inbox, just not pushed.
    assert the_conversation(client, seller)["needs_attention_at"] is not None


def test_nothing_is_queued_for_a_seller_with_no_linked_chat(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="sok_bot")
    with wh.sends(), alerts() as to_seller, ai.replies(f"Jam.\n{NEEDS_SELLER_MARKER}"):
        customer_says(client, integration, "discount?")
    assert to_seller == []
    from app.models.job import Job
    assert db.query(Job).filter(Job.kind == seller_alerts.SELLER_ALERT).count() == 0


def test_alerts_are_written_in_the_sellers_language(client, seller, db):
    integration = link(client, seller, db, language="km")
    with wh.sends(), alerts() as to_seller, ai.replies(f"Jam.\n{NEEDS_SELLER_MARKER}"):
        customer_says(client, integration, "discount?")
    assert "ត្រូវការអ្នក" in to_seller[0]["text"]


def test_language_is_saved_from_the_profile(client, seller):
    r = client.patch("/api/v1/auth/me", json={"language": "km"}, headers=seller.headers)
    assert r.json()["language"] == "km"
    assert client.patch("/api/v1/auth/me", json={"language": "fr"},
                        headers=seller.headers).status_code == 422


# ── The inbox ────────────────────────────────────────────────────────────────

def test_the_inbox_can_filter_and_count_threads_needing_attention(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies(f"Jam.\n{NEEDS_SELLER_MARKER}"):
        customer_says(client, integration, "discount?", chat_id=1, update_id=1)
    with wh.sends(), alerts(), ai.replies("Bat, 4 USD."):
        customer_says(client, integration, "tlai ponman?", chat_id=2, update_id=2)

    flagged = client.get("/api/v1/conversations/?needs_attention=true",
                         headers=seller.headers).json()
    assert len(flagged) == 1
    metrics = client.get("/api/v1/conversations/metrics", headers=seller.headers).json()
    assert metrics["needs_attention"] == 1

    # Closing the thread is also an answer of sorts.
    client.patch(f"/api/v1/conversations/{flagged[0]['id']}",
                 json={"status": "closed"}, headers=seller.headers)
    assert client.get("/api/v1/conversations/metrics",
                      headers=seller.headers).json()["needs_attention"] == 0
    assert db.query(Conversation).filter(Conversation.needs_attention_at.isnot(None)).count() == 0
