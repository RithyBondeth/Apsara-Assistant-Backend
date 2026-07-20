"""Conversation creation, platform validation, and message ordering tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from app.api.v1.endpoints.conversations import MESSAGE_WINDOW
from app.models.message import Message


def _make_customer(client):
    r = client.post("/api/v1/customers/", json={"name": "Sok Dara"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_conversation_accepts_supported_platforms(auth_client):
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)

    for platform in ("telegram", "messenger", "instagram", "website"):
        r = client.post(
            "/api/v1/conversations/",
            json={"customer_id": customer_id, "platform": platform},
        )
        assert r.status_code == 201, r.text
        assert r.json()["platform"] == platform


def test_create_conversation_rejects_unsupported_platform(auth_client):
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)

    # "facebook" and "tiktok" are not channels we support — the old web client
    # offered both, which silently persisted unusable rows.
    for platform in ("facebook", "tiktok", "", "TELEGRAM"):
        r = client.post(
            "/api/v1/conversations/",
            json={"customer_id": customer_id, "platform": platform},
        )
        assert r.status_code == 400, f"{platform!r} should be rejected: {r.text}"
        assert r.json()["detail"]["code"] == "unsupported_platform"

    assert client.get("/api/v1/conversations/").json()["items"] == []


def test_conversation_detail_returns_messages_chronologically(auth_client, db_session):
    """The chat window reads the detail endpoint, which joinedloads messages.

    Insertion order is deliberately the REVERSE of chronological order here:
    SQLite hands rows back in rowid order, so a relationship without an
    explicit order_by would return them newest-first and the test would fail.
    """
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)
    conv_id = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "website"},
    ).json()["id"]

    # Built directly rather than through POST /messages: that endpoint is the
    # seller-takeover path now and would try to deliver each one to a platform.
    # Insertion order is the reverse of chronological, and created_at is stamped
    # explicitly, so SQLite's rowid order can't accidentally satisfy the assert.
    base = datetime(2026, 7, 16, 9, 0, 0)
    minutes = {"first": 0, "second": 1, "third": 2, "fourth": 3}
    for content in ["fourth", "third", "second", "first"]:
        db_session.add(
            Message(
                conversation_id=UUID(conv_id),
                sender_type="customer",
                message_type="text",
                content=content,
                created_at=base + timedelta(minutes=minutes[content]),
            )
        )
    db_session.commit()

    detail = client.get(f"/api/v1/conversations/{conv_id}").json()
    assert [m["content"] for m in detail["messages"]] == [
        "first",
        "second",
        "third",
        "fourth",
    ]


def test_create_conversation_reuses_open_conversation(auth_client):
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)

    first = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "telegram"},
    ).json()
    second = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "telegram"},
    ).json()

    assert first["id"] == second["id"]


def test_conversation_detail_returns_only_the_newest_window(auth_client, db_session):
    """The detail endpoint must bound the thread it loads.

    It used to joinedload every message in the conversation, so opening a
    long-running channel thread pulled the whole history on every click. The
    window keeps the newest MESSAGE_WINDOW messages — the END of the thread,
    which is the part the seller is answering — and reports the true total so
    the UI can offer to page back.
    """
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)
    conv_id = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "website"},
    ).json()["id"]

    total = MESSAGE_WINDOW + 15
    base = datetime(2026, 7, 16, 9, 0, 0)
    for i in range(total):
        db_session.add(
            Message(
                conversation_id=UUID(conv_id),
                sender_type="customer",
                message_type="text",
                content=f"msg-{i:03d}",
                created_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    detail = client.get(f"/api/v1/conversations/{conv_id}").json()

    assert detail["message_total"] == total
    assert len(detail["messages"]) == MESSAGE_WINDOW
    # The NEWEST window, still oldest-first within itself. Sorting descending to
    # apply the limit and forgetting to flip back would fail both asserts.
    assert detail["messages"][0]["content"] == f"msg-{total - MESSAGE_WINDOW:03d}"
    assert detail["messages"][-1]["content"] == f"msg-{total - 1:03d}"


def test_older_messages_are_reachable_through_the_messages_route(auth_client, db_session):
    """The window is only safe because the paginated route can reach past it.

    Mirrors what the chat window's "load older" does: ask for the page directly
    before the window it already has.
    """
    client, _token, _uid = auth_client
    customer_id = _make_customer(client)
    conv_id = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "website"},
    ).json()["id"]

    total = MESSAGE_WINDOW + 15
    base = datetime(2026, 7, 16, 9, 0, 0)
    for i in range(total):
        db_session.add(
            Message(
                conversation_id=UUID(conv_id),
                sender_type="customer",
                message_type="text",
                content=f"msg-{i:03d}",
                created_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    older = client.get(
        f"/api/v1/conversations/{conv_id}/messages",
        params={"skip": 0, "limit": total - MESSAGE_WINDOW},
    ).json()

    assert older["total"] == total
    # Exactly the messages the window left out, and they butt up against it.
    assert [m["content"] for m in older["items"]] == [
        f"msg-{i:03d}" for i in range(total - MESSAGE_WINDOW)
    ]
