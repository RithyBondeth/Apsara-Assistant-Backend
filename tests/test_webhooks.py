"""The inbound webhooks — the only unauthenticated endpoints in the API."""

import json
import uuid

import pytest

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.services import ai_service
from tests import ai, webhooks as wh


def messages_for(db, seller_headers=None):
    return db.query(Message).order_by(Message.created_at).all()


def conversation_of(client, seller):
    convs = client.get("/api/v1/conversations/", headers=seller.headers).json()
    assert len(convs) == 1, convs
    return convs[0]


def thread(client, seller):
    conv = conversation_of(client, seller)
    return client.get(f"/api/v1/conversations/{conv['id']}/messages",
                      headers=seller.headers).json()


# ── Messenger: proving the caller is Meta ────────────────────────────────────

def test_subscription_handshake_echoes_the_challenge(client):
    r = client.get(wh.MESSENGER_URL, params={
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 200
    assert r.text == "1158201444"


def test_handshake_refuses_a_wrong_verify_token(client):
    r = client.get(wh.MESSENGER_URL, params={
        "hub.mode": "subscribe",
        "hub.verify_token": "not-the-token",
        "hub.challenge": "123",
    })
    assert r.status_code == 403


def test_unsigned_payloads_are_refused(client, seller):
    wh.connect(client, seller)
    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"),
                              signature=None)
    assert r.status_code == 403
    assert sent == []


@pytest.mark.parametrize("bad", [
    "sha256=deadbeef",
    "sha1=abc",
    "deadbeef",
    "sha256=",
])
def test_bad_signatures_are_refused(client, seller, db, bad):
    wh.connect(client, seller)
    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"),
                              signature=bad)
    assert r.status_code == 403
    assert sent == []
    assert db.query(Message).count() == 0


def test_signature_is_checked_against_the_bytes_received(client, seller, db):
    """Signed one body, sent another — the digest must not survive it."""
    wh.connect(client, seller)
    signed = json.dumps(wh.messenger_payload("page-1", "psid-1", "hi")).encode()
    tampered = json.dumps(wh.messenger_payload("page-1", "psid-1", "different")).encode()

    r = client.post(wh.MESSENGER_URL, content=tampered,
                    headers={"Content-Type": "application/json",
                             "X-Hub-Signature-256": wh.sign(signed)})

    assert r.status_code == 403
    assert db.query(Message).count() == 0


# ── Messenger: the happy path ────────────────────────────────────────────────

def test_a_customer_message_is_recorded_and_answered(client, seller, db):
    wh.connect(client, seller, external_id="page-1")
    seller.product("Silk Scarf", "12.50", 8)

    with wh.sends() as sent, ai.replies("Tlai 12.50 USD.") as captured:
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1",
                                                           "tlai ponman?"))

    assert r.status_code == 200
    stored = thread(client, seller)
    assert [(m["sender_type"], m["content"]) for m in stored] == [
        ("customer", "tlai ponman?"),
        ("assistant", "Tlai 12.50 USD."),
    ]
    assert sent == [{"platform": "messenger", "recipient_id": "psid-1",
                     "text": "Tlai 12.50 USD.", "token": mock_token(db)}]
    # The seller's catalogue reached the model.
    assert "Silk Scarf" in captured.system_prompt


def mock_token(db):
    from app.models.platform_connection import PlatformConnection
    return db.query(PlatformConnection).first().access_token


def test_an_unknown_sender_becomes_a_customer(client, seller, db):
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-99", "hello"))

    customer = db.query(Customer).one()
    assert customer.platform == "messenger"
    assert customer.platform_id == "psid-99"
    assert customer.user_id is not None


def test_a_returning_sender_reuses_their_customer_and_thread(client, seller, db):
    wh.connect(client, seller, external_id="page-1")

    for i, text in enumerate(["first", "second"]):
        with wh.sends(), ai.replies(f"reply {i}"):
            wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", text,
                                                           mid=f"mid.{i}"))

    assert db.query(Customer).count() == 1
    assert db.query(Conversation).count() == 1
    assert len(thread(client, seller)) == 4


def test_a_closed_thread_is_left_closed_and_a_new_one_opens(client, seller, db):
    """Matches the conversations endpoint: a finished thread stays a record."""
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "first"))

    first = conversation_of(client, seller)
    client.patch(f"/api/v1/conversations/{first['id']}", json={"status": "closed"},
                 headers=seller.headers)

    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "again",
                                                       mid="mid.2"))

    conversations = client.get("/api/v1/conversations/", headers=seller.headers).json()
    assert len(conversations) == 2
    statuses = {c["id"]: c["status"] for c in conversations}
    assert statuses[first["id"]] == "closed"


# ── Messenger: what must never be treated as a customer message ──────────────

def test_the_pages_own_echoes_are_ignored(client, seller, db):
    """An echo is the page's outgoing message fed back. Answering one would
    have the assistant reply to itself, and then to that reply."""
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, wh.messenger_payload(
            "page-1", "psid-1", "our own reply", is_echo=True))

    assert r.status_code == 200
    assert sent == []
    assert db.query(Message).count() == 0


@pytest.mark.parametrize("event", [
    {"delivery": {"mids": ["mid.1"], "watermark": 1}},
    {"read": {"watermark": 1}},
    {"postback": {"payload": "GET_STARTED"}},
    {"message": {"mid": "mid.2", "attachments": [{"type": "image"}]}},
])
def test_non_message_events_are_acknowledged_and_dropped(client, seller, db, event):
    wh.connect(client, seller, external_id="page-1")
    payload = {"object": "page", "entry": [{"id": "page-1", "messaging": [
        {"sender": {"id": "psid-1"}, "recipient": {"id": "page-1"}, **event}]}]}

    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, payload)

    assert r.status_code == 200
    assert sent == []
    assert db.query(Message).count() == 0


def test_a_redelivery_does_not_produce_a_second_reply(client, seller, db):
    """Meta retries anything it does not see acknowledged promptly."""
    wh.connect(client, seller, external_id="page-1")
    payload = wh.messenger_payload("page-1", "psid-1", "tlai ponman?", mid="mid.same")

    for _ in range(3):
        with wh.sends() as sent, ai.replies("once"):
            wh.post_messenger(client, payload)

    assert len(thread(client, seller)) == 2
    assert sent == [], "the repeat delivery must not have been answered"


def test_an_unconnected_page_is_acknowledged_not_retried(client, seller, db):
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, wh.messenger_payload("page-other", "psid-1", "hi"))

    assert r.status_code == 200, "a 4xx here would have Meta retry for hours"
    assert sent == []
    assert db.query(Message).count() == 0


def test_a_deactivated_connection_stops_receiving(client, seller, db):
    integration = wh.connect(client, seller, external_id="page-1")
    client.patch(f"/api/v1/integrations/{integration['id']}", json={"is_active": False},
                 headers=seller.headers)

    with wh.sends() as sent, ai.replies():
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))

    assert r.status_code == 200
    assert sent == []
    assert db.query(Message).count() == 0


# ── Degraded paths ───────────────────────────────────────────────────────────

def test_auto_reply_off_records_without_answering(client, seller, db):
    integration = wh.connect(client, seller, external_id="page-1")
    client.patch(f"/api/v1/integrations/{integration['id']}", json={"auto_reply": False},
                 headers=seller.headers)

    with wh.sends() as sent, ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hello"))

    stored = thread(client, seller)
    assert [m["sender_type"] for m in stored] == ["customer"]
    assert sent == []


def test_an_ai_outage_still_records_the_customer(client, seller, db):
    from openai import APIConnectionError
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, ai.fails(APIConnectionError(request=None)):
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hello?"))

    assert r.status_code == 200
    stored = thread(client, seller)
    assert [(m["sender_type"], m["content"]) for m in stored] == [("customer", "hello?")]
    assert sent == []


def test_an_undelivered_reply_is_not_written_to_the_thread(client, seller, db):
    """The customer never saw it, so showing it to the seller would be a lie."""
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(ok=False) as sent, ai.replies("never arrives"):
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))

    assert r.status_code == 200
    assert len(sent) == 1, "delivery was attempted"
    stored = thread(client, seller)
    assert [m["sender_type"] for m in stored] == ["customer"]


# ── Telegram ─────────────────────────────────────────────────────────────────

def test_telegram_message_is_recorded_and_answered(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram",
                             external_id="bot-1", token="bot-token")

    with wh.sends() as sent, ai.replies("Sur sdei!"):
        r = wh.post_telegram(client, integration["id"],
                             wh.telegram_update(555, "sok sabay?"),
                             secret=integration["webhook_secret"])

    assert r.status_code == 200
    stored = thread(client, seller)
    assert [(m["sender_type"], m["content"]) for m in stored] == [
        ("customer", "sok sabay?"), ("assistant", "Sur sdei!")]
    assert sent[0]["platform"] == "telegram"
    assert sent[0]["recipient_id"] == "555"


def test_telegram_uses_the_senders_name(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    with wh.sends(), ai.replies():
        wh.post_telegram(client, integration["id"],
                         wh.telegram_update(555, "hello", last_name="Neang"),
                         secret=integration["webhook_secret"])

    assert db.query(Customer).one().name == "Srey Neang"


@pytest.mark.parametrize("secret", [None, "", "wrong-secret"])
def test_telegram_refuses_a_bad_secret(client, seller, db, secret):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    with wh.sends() as sent, ai.replies():
        r = wh.post_telegram(client, integration["id"], wh.telegram_update(555, "hi"),
                             secret=secret)

    assert r.status_code == 403
    assert sent == []
    assert db.query(Message).count() == 0


def test_an_unknown_connection_answers_like_a_bad_secret(client, seller):
    """Same response either way, so the endpoint cannot be used to discover
    which connection ids exist."""
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    unknown = client.post(f"/api/v1/webhooks/telegram/{uuid.uuid4()}",
                          json=wh.telegram_update(555, "hi"),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "anything"})
    wrong = wh.post_telegram(client, integration["id"], wh.telegram_update(555, "hi"),
                             secret="wrong")

    assert unknown.status_code == wrong.status_code == 403
    assert unknown.json() == wrong.json()


@pytest.mark.parametrize("payload", [
    {"update_id": 1, "edited_message": {"text": "changed", "chat": {"id": 1},
                                        "from": {"id": 1, "is_bot": False}}},
    {"update_id": 2, "message": {"chat": {"id": 1}, "from": {"id": 1, "is_bot": False},
                                 "photo": [{"file_id": "x"}]}},
    {"update_id": 3, "message": {"text": "hi", "chat": {"id": 1},
                                 "from": {"id": 1, "is_bot": True}}},
    {"update_id": 4},
])
def test_telegram_ignores_what_it_cannot_answer(client, seller, db, payload):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    with wh.sends() as sent, ai.replies():
        r = wh.post_telegram(client, integration["id"], payload,
                             secret=integration["webhook_secret"])

    assert r.status_code == 200
    assert sent == []
    assert db.query(Message).count() == 0


def test_telegram_redelivery_is_ignored(client, seller, db):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    update = wh.telegram_update(555, "tlai ponman?", update_id=4242)

    for _ in range(3):
        with wh.sends(), ai.replies("once"):
            wh.post_telegram(client, integration["id"], update,
                             secret=integration["webhook_secret"])

    assert len(thread(client, seller)) == 2


def test_two_sellers_bots_stay_separate(client, seller, other_seller, db):
    mine = wh.connect(client, seller, platform="telegram", external_id="bot-mine")
    theirs = wh.connect(client, other_seller, platform="telegram", external_id="bot-theirs")

    with wh.sends(), ai.replies():
        wh.post_telegram(client, mine["id"], wh.telegram_update(1, "for me"),
                         secret=mine["webhook_secret"])
        wh.post_telegram(client, theirs["id"], wh.telegram_update(2, "for them"),
                         secret=theirs["webhook_secret"])

    assert len(client.get("/api/v1/conversations/", headers=seller.headers).json()) == 1
    assert len(client.get("/api/v1/conversations/",
                          headers=other_seller.headers).json()) == 1
    # One seller's secret must not open the other's endpoint.
    r = wh.post_telegram(client, theirs["id"], wh.telegram_update(3, "crossed"),
                         secret=mine["webhook_secret"])
    assert r.status_code == 403


# ── Messenger customer names ─────────────────────────────────────────────────

@pytest.fixture
def profile(monkeypatch):
    """Stub the Graph profile lookup, recording the psids it was asked about."""
    asked = []

    def _fetch(token, psid):
        asked.append(psid)
        return _fetch.name

    _fetch.name = "Srey Neang"
    import app.services.inbound as inbound
    monkeypatch.setattr(inbound, "fetch_messenger_profile", _fetch)
    _fetch.asked = asked
    return _fetch


def test_a_messenger_customer_is_named_from_their_profile(client, seller, db, profile):
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))

    assert db.query(Customer).one().name == "Srey Neang"
    assert profile.asked == ["psid-1"]


def test_the_profile_is_only_looked_up_on_first_contact(client, seller, db, profile):
    wh.connect(client, seller, external_id="page-1")

    for i in range(3):
        with wh.sends(), ai.replies():
            wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", f"m{i}",
                                                           mid=f"mid.{i}"))

    assert profile.asked == ["psid-1"], "one lookup, not one per message"


def test_a_missing_profile_falls_back_to_a_placeholder(client, seller, db, profile):
    """The lookup needs a permission the customer may not have granted."""
    profile.name = None
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-123456", "hi"))

    assert db.query(Customer).one().name == "Customer 123456"


def test_telegram_needs_no_lookup(client, seller, db, profile):
    """Its payload carries the name already."""
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    with wh.sends(), ai.replies():
        wh.post_telegram(client, integration["id"], wh.telegram_update(555, "hi"),
                         secret=integration["webhook_secret"])

    assert profile.asked == []
    assert db.query(Customer).one().name == "Srey"


def test_a_name_the_seller_set_is_not_overwritten(client, seller, db, profile):
    """Telegram sends a name on every message; a seller's correction must win."""
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    with wh.sends(), ai.replies():
        wh.post_telegram(client, integration["id"],
                         wh.telegram_update(555, "hi", update_id=1),
                         secret=integration["webhook_secret"])

    customer = db.query(Customer).one()
    client.patch(f"/api/v1/customers/{customer.id}", json={"name": "Neang (VIP)"},
                 headers=seller.headers)

    with wh.sends(), ai.replies():
        wh.post_telegram(client, integration["id"],
                         wh.telegram_update(555, "again", update_id=2),
                         secret=integration["webhook_secret"])

    db.expire_all()
    assert db.query(Customer).one().name == "Neang (VIP)"


def test_seller_can_reply_to_the_exact_connected_channel(client, seller, db, monkeypatch):
    integration = wh.connect(client, seller, external_id="page-1")
    client.patch(
        f"/api/v1/integrations/{integration['id']}",
        json={"auto_reply": False},
        headers=seller.headers,
    )
    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hello"))

    conversation = conversation_of(client, seller)
    assert conversation["platform_connection_id"] == integration["id"]
    assert conversation["source"] == "channel"
    sent = []

    def deliver(platform, token, recipient_id, text):
        sent.append((platform, recipient_id, text))
        return True

    import app.api.v1.endpoints.conversations as conversations_endpoint
    monkeypatch.setattr(conversations_endpoint, "send_reply", deliver)
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "I can help with that."},
        headers=seller.headers,
    )

    assert response.status_code == 201, response.text
    assert sent == [("messenger", "psid-1", "I can help with that.")]
    assert response.json()["sender_type"] == "seller"


def test_disconnected_channel_never_turns_into_an_ai_rehearsal(client, seller, db):
    integration = wh.connect(client, seller, external_id="page-1")
    client.patch(
        f"/api/v1/integrations/{integration['id']}",
        json={"auto_reply": False},
        headers=seller.headers,
    )
    with wh.sends(), ai.replies():
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hello"))
    conversation = conversation_of(client, seller)
    client.delete(f"/api/v1/integrations/{integration['id']}", headers=seller.headers)

    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "Are you still there?"},
        headers=seller.headers,
    )

    assert response.status_code == 409
    assert "unavailable" in response.json()["detail"]
    assert [message.sender_type for message in messages_for(db)] == ["customer"]


# ── The shop's payment QR ────────────────────────────────────────────────────

QR = "https://cdn.example/qr.png"
PAY_NOW = f"Scan this to pay.\n{ai_service.PAYMENT_QR_MARKER}"


def set_qr(client, seller, url=QR):
    r = client.patch("/api/v1/auth/me", json={"payment_qr_url": url},
                     headers=seller.headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_qr_follows_the_reply_as_its_own_message(client, seller, db):
    set_qr(client, seller)
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, wh.sends_images() as images, ai.replies(PAY_NOW):
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1",
                                                           "I'll take it"))

    assert r.status_code == 200
    assert [s["text"] for s in sent] == ["Scan this to pay."]
    assert [i["image_url"] for i in images] == [QR]
    assert images[0]["recipient_id"] == "psid-1"

    stored = thread(client, seller)
    assert [(m["sender_type"], m["message_type"]) for m in stored] == [
        ("customer", "text"), ("assistant", "text"), ("assistant", "image")]
    assert [a["file_url"] for a in stored[-1]["attachments"]] == [QR]


def test_the_marker_never_reaches_a_customer_of_a_seller_without_a_qr(client, seller, db):
    """The prompt does not offer the marker to them, but a model may emit it anyway."""
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, wh.sends_images() as images, ai.replies(PAY_NOW):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "how to pay?"))

    assert [s["text"] for s in sent] == ["Scan this to pay."]
    assert images == []
    assert thread(client, seller)[-1]["message_type"] == "text"


def test_a_qr_cleared_after_the_prompt_was_built_is_not_sent(client, seller, db):
    set_qr(client, seller)
    wh.connect(client, seller, external_id="page-1")
    set_qr(client, seller, url=None)

    with wh.sends(), wh.sends_images() as images, ai.replies(PAY_NOW):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "pay?"))

    assert images == []


def test_an_undelivered_reply_takes_the_qr_with_it(client, seller, db):
    """A QR with no message explaining it is worse than nothing."""
    set_qr(client, seller)
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(ok=False), wh.sends_images() as images, ai.replies(PAY_NOW):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "pay?"))

    assert images == []
    assert [m["sender_type"] for m in thread(client, seller)] == ["customer"]


def test_an_undelivered_qr_leaves_the_reply_standing(client, seller, db):
    """The customer did read the text, so the thread must still show it."""
    set_qr(client, seller)
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(), wh.sends_images(ok=False) as images, ai.replies(PAY_NOW):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "pay?"))

    assert len(images) == 1, "delivery was attempted"
    stored = thread(client, seller)
    assert [(m["sender_type"], m["message_type"]) for m in stored] == [
        ("customer", "text"), ("assistant", "text")]
