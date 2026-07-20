"""Webhook tests: routing, secret verification, and AI reply wiring (mocked)."""
from __future__ import annotations

import hashlib
import hmac
import json

import app.services.chat_service as chat_service
import app.services.instagram as instagram_service
import app.services.messenger as messenger_service
import app.services.telegram as telegram_service


def _make_telegram_integration(client) -> str:
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "telegram", "access_token": "bot-token", "secret_token": "s3cr3t"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_telegram_webhook_creates_conversation_and_replies(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _make_telegram_integration(client)

    async def fake_ai(messages):
        return "សួស្តី! (auto reply)"

    sent = []

    async def fake_send(access_token, chat_id, text):
        sent.append((access_token, chat_id, text))

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)

    update = {"message": {"chat": {"id": 555, "first_name": "Dara"}, "text": "tinh nhom"}}
    r = client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "handled": True}

    # the reply was pushed back to Telegram
    assert len(sent) == 1
    assert sent[0][1] == "555"
    assert "auto reply" in sent[0][2]

    # a conversation with 2 messages (customer + assistant) now exists
    convs = client.get("/api/v1/conversations/").json()["items"]
    assert len(convs) == 1
    msgs = client.get(f"/api/v1/conversations/{convs[0]['id']}/messages").json()["items"]
    kinds = [m["sender_type"] for m in msgs]
    assert kinds == ["customer", "assistant"]


def test_telegram_webhook_is_idempotent_on_redelivery(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _make_telegram_integration(client)

    async def fake_ai(messages):
        return "reply"

    sent = []

    async def fake_send(access_token, chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)

    # Same update_id delivered twice (platform redelivery)
    update = {
        "update_id": 100200,
        "message": {"chat": {"id": 555, "first_name": "Dara"}, "text": "hello"},
    }
    headers = {"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"}
    first = client.post(f"/api/v1/webhooks/telegram/{integration_id}", json=update, headers=headers)
    second = client.post(f"/api/v1/webhooks/telegram/{integration_id}", json=update, headers=headers)

    assert first.json() == {"ok": True, "handled": True}
    assert second.json() == {"ok": True, "handled": False, "paused": False}  # deduped

    # Only one reply sent, and the conversation has exactly 2 messages (not 4)
    assert len(sent) == 1
    convs = client.get("/api/v1/conversations/").json()["items"]
    assert len(convs) == 1
    msgs = client.get(f"/api/v1/conversations/{convs[0]['id']}/messages").json()["items"]
    assert len(msgs) == 2


def test_telegram_webhook_rejects_bad_secret(auth_client):
    client, _token, _uid = auth_client
    integration_id = _make_telegram_integration(client)
    r = client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={"message": {"chat": {"id": 1}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 403


def test_telegram_webhook_unknown_integration_404(auth_client):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/webhooks/telegram/00000000-0000-0000-0000-000000000000",
        json={"message": {"chat": {"id": 1}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "x"},
    )
    assert r.status_code == 404


def test_messenger_webhook_resolves_real_customer_name(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "messenger",
            "access_token": "page-token",
            "secret_token": "verifyme",
            "app_secret": "app-secret",
        },
    )
    integration_id = r.json()["id"]

    async def fake_ai(messages):
        return "reply"

    async def fake_send(access_token, recipient_id, text):
        pass

    async def fake_profile(access_token, psid):
        return "Sok Dara"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(messenger_service, "send_message", fake_send)
    monkeypatch.setattr(messenger_service, "get_profile_name", fake_profile)

    payload = {"entry": [{"messaging": [
        {"sender": {"id": "PSID123"}, "message": {"mid": "m1", "text": "hello"}}
    ]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()

    resp = client.post(
        f"/api/v1/webhooks/messenger/{integration_id}",
        content=raw,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    # customer was created with the resolved Graph name, not the PSID placeholder
    customers = client.get("/api/v1/customers/").json()["items"]
    assert len(customers) == 1
    assert customers[0]["name"] == "Sok Dara"
    assert customers[0]["platform_id"] == "PSID123"


def test_instagram_webhook_end_to_end(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "instagram",
            "access_token": "ig-token",
            "secret_token": "ig-verify",
            "app_secret": "ig-app-secret",
        },
    )
    assert r.status_code == 201, r.text
    integration_id = r.json()["id"]

    async def fake_ai(messages):
        return "សួស្តី"

    sent = []

    async def fake_send(access_token, recipient_id, text):
        sent.append((recipient_id, text))

    async def fake_profile(access_token, igsid):
        return "Insta Dara"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(instagram_service, "send_message", fake_send)
    monkeypatch.setattr(instagram_service, "get_profile_name", fake_profile)

    payload = {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": "IGSID9"}, "message": {"mid": "ig1", "text": "hello"}}
    ]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"ig-app-secret", raw, hashlib.sha256).hexdigest()

    resp = client.post(
        f"/api/v1/webhooks/instagram/{integration_id}",
        content=raw,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.text == "EVENT_RECEIVED"
    assert sent == [("IGSID9", "សួស្តី")]

    # customer created with the resolved IG name; conversation platform = instagram
    customers = client.get("/api/v1/customers/").json()["items"]
    assert customers[0]["name"] == "Insta Dara"
    convs = client.get("/api/v1/conversations/").json()["items"]
    assert convs[0]["platform"] == "instagram"


def test_instagram_webhook_bad_signature_rejected(auth_client):
    client, _token, _uid = auth_client
    integration_id = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "instagram",
            "access_token": "ig-token",
            "secret_token": "ig-verify",
            "app_secret": "ig-app-secret",
        },
    ).json()["id"]
    resp = client.post(
        f"/api/v1/webhooks/instagram/{integration_id}",
        content=b'{"object":"instagram","entry":[]}',
        headers={"X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"},
    )
    assert resp.status_code == 403


def test_instagram_requires_app_secret(auth_client):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "instagram", "access_token": "ig-token", "secret_token": "v"},
    )
    assert r.status_code == 400


def test_messenger_verify_handshake(auth_client):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "messenger",
            "access_token": "page-token",
            "secret_token": "verifyme",
            "app_secret": "app-secret",
        },
    )
    integration_id = r.json()["id"]

    ok = client.get(
        f"/api/v1/webhooks/messenger/{integration_id}",
        params={"hub.mode": "subscribe", "hub.verify_token": "verifyme", "hub.challenge": "42"},
    )
    assert ok.status_code == 200
    assert ok.text == "42"

    bad = client.get(
        f"/api/v1/webhooks/messenger/{integration_id}",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
    )
    assert bad.status_code == 403
