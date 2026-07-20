"""The seller only steps in if something tells them to.

Human takeover is worthless without this: nothing else in the product says
"a customer is waiting" or "the AI gave up".
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import app.services.ai_service as ai_service
import app.services.chat_service as chat_service
import app.services.telegram as telegram_service
from app.api.v1.endpoints.conversations import _needs_me_clause
from app.models.conversation import Conversation
from app.services.ai_service import NEEDS_SELLER_MARKER, split_needs_seller


def _telegram_integration(client) -> str:
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "telegram", "access_token": "bot-token", "secret_token": "s3cr3t"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _inbound(client, integration_id, text="hello", update_id=1):
    return client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={
            "update_id": update_id,
            "message": {"chat": {"id": 555, "first_name": "Dara"}, "text": text},
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )


def _patch(monkeypatch, reply="sure, $24.50"):
    async def fake_ai(messages):
        return reply

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        pass

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)


# ── The escalation marker ─────────────────────────────────────────────────────


def test_marker_is_stripped_before_the_customer_sees_it():
    text, flagged = split_needs_seller(f"I'll check with the seller. {NEEDS_SELLER_MARKER}")
    assert flagged is True
    assert NEEDS_SELLER_MARKER not in text
    assert text == "I'll check with the seller."


def test_marker_is_stripped_even_mid_reply():
    """Defensive: the model is told to append it, but a leaked marker must
    never reach the customer wherever it lands."""
    text, flagged = split_needs_seller(f"one {NEEDS_SELLER_MARKER} two")
    assert flagged is True
    assert NEEDS_SELLER_MARKER not in text


def test_ordinary_reply_is_untouched():
    text, flagged = split_needs_seller("It is $24.50")
    assert (text, flagged) == ("It is $24.50", False)


def test_the_prompt_actually_tells_the_model_the_marker(auth_client):
    from app.models.user import User

    prompt = ai_service.build_system_prompt(
        User(business_name="Dara Silk", full_name="Sok Dara"), [], "khmer"
    )
    assert NEEDS_SELLER_MARKER in prompt


# ── Flagging ──────────────────────────────────────────────────────────────────


def test_escalating_reply_flags_the_thread_and_hides_the_marker(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch, reply=f"I'll ask the seller. {NEEDS_SELLER_MARKER}")

    _inbound(client, integration_id, text="can I get a discount?")

    conv = client.get("/api/v1/conversations/").json()["items"][0]
    assert conv["needs_attention"] is True

    msgs = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()["items"]
    assert NEEDS_SELLER_MARKER not in (msgs[-1]["content"] or "")
    assert msgs[-1]["content"] == "I'll ask the seller."


def test_ordinary_reply_does_not_flag(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    assert conv["needs_attention"] is False


def test_ai_failure_keeps_the_message_and_raises_a_flag(auth_client, monkeypatch):
    """This used to roll the customer's message back. The webhook acks 200
    either way, so the platform never redelivered — the message was destroyed
    and nobody, customer or seller, ever found out."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    async def broken_ai(messages):
        raise RuntimeError("openai is down")

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        pass

    monkeypatch.setattr(chat_service, "generate_ai_reply", broken_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)

    r = _inbound(client, integration_id, text="តើអ្នកមានក្រមាទេ?")
    assert r.status_code == 200  # still acked; retrying wouldn't help

    convs = client.get("/api/v1/conversations/").json()["items"]
    assert len(convs) == 1
    assert convs[0]["needs_attention"] is True

    # The customer's words survived, so the seller can answer them by hand.
    msgs = client.get(f"/api/v1/conversations/{convs[0]['id']}/messages").json()["items"]
    assert [m["content"] for m in msgs] == ["តើអ្នកមានក្រមាទេ?"]


# ── Unread ────────────────────────────────────────────────────────────────────


def test_new_conversation_from_a_customer_is_unread(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    assert conv["unread"] is True


def test_marking_seen_clears_unread_and_attention(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch, reply=f"asking the seller {NEEDS_SELLER_MARKER}")

    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    assert conv["unread"] is True and conv["needs_attention"] is True

    r = client.post(f"/api/v1/conversations/{conv['id']}/seen")
    assert r.status_code == 200, r.text
    assert r.json()["unread"] is False
    assert r.json()["needs_attention"] is False

    # ...and it stays cleared on re-read.
    assert client.get("/api/v1/conversations/").json()["items"][0]["unread"] is False


def test_a_new_customer_message_makes_it_unread_again(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id, update_id=1)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    client.post(f"/api/v1/conversations/{conv['id']}/seen")
    assert client.get("/api/v1/conversations/").json()["items"][0]["unread"] is False

    _inbound(client, integration_id, text="are you there?", update_id=2)
    assert client.get("/api/v1/conversations/").json()["items"][0]["unread"] is True


def test_the_sellers_own_reply_does_not_mark_it_unread(auth_client, monkeypatch):
    """Unread keys off the CUSTOMER's messages. If the seller's own reply (or
    the AI's) counted, every thread would be unread forever and the signal
    would be worthless."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    client.post(f"/api/v1/conversations/{conv['id']}/seen")

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi there"}
    )
    assert r.status_code == 201, r.text
    assert client.get("/api/v1/conversations/").json()["items"][0]["unread"] is False


def test_paused_conversation_still_goes_unread(auth_client, monkeypatch):
    """A human took over — these are exactly the ones they must not miss."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id, update_id=1)
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    client.patch(f"/api/v1/conversations/{conv['id']}", json={"ai_enabled": False})
    client.post(f"/api/v1/conversations/{conv['id']}/seen")

    _inbound(client, integration_id, text="hello?", update_id=2)
    assert client.get("/api/v1/conversations/").json()["items"][0]["unread"] is True


# ── Filter + count ────────────────────────────────────────────────────────────


def test_needs_me_filter_and_badge_count_agree(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)
    _patch(monkeypatch)

    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()["items"][0]

    listed = client.get("/api/v1/conversations/?needs_me=true").json()["items"]
    stats = client.get("/api/v1/dashboard/stats").json()
    assert len(listed) == 1
    assert stats["needs_me_conversations"] == 1

    client.post(f"/api/v1/conversations/{conv['id']}/seen")

    assert client.get("/api/v1/conversations/?needs_me=true").json()["items"] == []
    assert client.get("/api/v1/dashboard/stats").json()["needs_me_conversations"] == 0


def test_sql_filter_matches_the_unread_property(auth_client, db_session):
    """_needs_me_clause() duplicates Conversation.unread in SQL because a
    Python property can't go in a WHERE. Pin them together so they can't drift.
    """
    client, _token, uid = auth_client
    cust = client.post("/api/v1/customers/", json={"name": "X"}).json()

    now = datetime(2026, 7, 16, 12, 0, 0)
    cases = [
        # (last_customer_message_at, last_seen_at) -> expected unread
        (None, None),
        (now, None),
        (now, now - timedelta(minutes=1)),
        (now, now + timedelta(minutes=1)),
        (None, now),
    ]
    made = []
    for last_msg, seen in cases:
        c = Conversation(
            user_id=UUID(uid),
            customer_id=UUID(cust["id"]),
            platform="website",
            status="open",
            last_customer_message_at=last_msg,
            last_seen_at=seen,
            needs_attention=False,
        )
        db_session.add(c)
        made.append(c)
    db_session.commit()

    # What SQL thinks needs the seller...
    sql_ids = {
        c.id
        for c in db_session.query(Conversation)
        .filter(Conversation.user_id == UUID(uid), _needs_me_clause())
        .all()
    }
    # ...must be exactly what the model thinks.
    py_ids = {c.id for c in made if c.unread}

    assert sql_ids == py_ids
    assert py_ids  # the fixture would be vacuous otherwise
