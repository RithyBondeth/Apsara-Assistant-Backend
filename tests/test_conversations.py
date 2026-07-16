"""Conversation creation, platform validation, and message ordering tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

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
        assert "Unsupported platform" in r.json()["detail"]

    assert client.get("/api/v1/conversations/").json() == []


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

    inserted = ["fourth", "third", "second", "first"]
    for content in inserted:
        r = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={
                "conversation_id": conv_id,
                "sender_type": "customer",
                "content": content,
            },
        )
        assert r.status_code == 201, r.text

    # Stamp created_at so the newest row was inserted first.
    base = datetime(2026, 7, 16, 9, 0, 0)
    rows = db_session.query(Message).filter(Message.conversation_id == UUID(conv_id)).all()
    by_content = {m.content: m for m in rows}
    for offset, content in enumerate(["first", "second", "third", "fourth"]):
        by_content[content].created_at = base + timedelta(minutes=offset)
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
