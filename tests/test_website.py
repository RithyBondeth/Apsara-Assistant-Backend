"""Website widget (synchronous chat) tests."""
from __future__ import annotations

import pytest

import app.api.v1.endpoints.webhooks as webhooks
import app.services.chat_service as chat_service


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The website limiter is a module singleton — clear it between tests."""
    webhooks.website_limiter.clear()
    yield
    webhooks.website_limiter.clear()


def _website_integration(client, secret_token=None):
    body = {"platform": "website", "access_token": "public-widget-key"}
    if secret_token is not None:
        body["secret_token"] = secret_token
    r = client.post("/api/v1/integrations/", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_website_chat_returns_reply_synchronously(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _website_integration(client)

    async def fake_ai(messages):
        return "How can I help you?"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)

    r = client.post(
        f"/api/v1/webhooks/website/{integration_id}",
        json={"session_id": "sess-1", "message": "hi", "name": "Visitor A"},
    )
    assert r.status_code == 200
    assert r.json() == {"reply": "How can I help you?", "paused": False}

    # a website customer + conversation were created and persisted
    customers = client.get("/api/v1/customers/").json()
    assert customers[0]["name"] == "Visitor A"
    assert customers[0]["platform"] == "website"
    convs = client.get("/api/v1/conversations/").json()
    assert convs[0]["platform"] == "website"
    msgs = client.get(f"/api/v1/conversations/{convs[0]['id']}/messages").json()
    assert [m["sender_type"] for m in msgs] == ["customer", "assistant"]


def test_website_chat_reuses_conversation_per_session(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _website_integration(client)

    async def fake_ai(messages):
        return "ok"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)

    url = f"/api/v1/webhooks/website/{integration_id}"
    client.post(url, json={"session_id": "sess-9", "message": "first"})
    client.post(url, json={"session_id": "sess-9", "message": "second"})

    # same session → one conversation, 4 messages (2 customer + 2 assistant)
    convs = client.get("/api/v1/conversations/").json()
    assert len(convs) == 1
    msgs = client.get(f"/api/v1/conversations/{convs[0]['id']}/messages").json()
    assert len(msgs) == 4


def test_website_origin_allowlist_enforced(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _website_integration(client, secret_token="https://shop.example.com")

    async def fake_ai(messages):
        return "ok"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)

    url = f"/api/v1/webhooks/website/{integration_id}"
    # disallowed / missing origin → 403
    assert client.post(url, json={"session_id": "s", "message": "hi"}).status_code == 403
    assert client.post(
        url, json={"session_id": "s", "message": "hi"}, headers={"Origin": "https://evil.com"}
    ).status_code == 403
    # allowed origin → 200
    assert client.post(
        url,
        json={"session_id": "s", "message": "hi"},
        headers={"Origin": "https://shop.example.com"},
    ).status_code == 200


def test_website_rate_limited(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _website_integration(client)

    async def fake_ai(messages):
        return "ok"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    # tighten the limit for the test
    monkeypatch.setattr(webhooks.website_limiter, "max_requests", 2)

    url = f"/api/v1/webhooks/website/{integration_id}"
    body = {"session_id": "s", "message": "hi"}
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 200
    # third within the window is rejected
    assert client.post(url, json=body).status_code == 429


def test_website_rate_limit_is_per_integration(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    a = _website_integration(client)
    b = _website_integration(client)

    async def fake_ai(messages):
        return "ok"

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(webhooks.website_limiter, "max_requests", 1)

    body = {"session_id": "s", "message": "hi"}
    # each integration gets its own budget
    assert client.post(f"/api/v1/webhooks/website/{a}", json=body).status_code == 200
    assert client.post(f"/api/v1/webhooks/website/{b}", json=body).status_code == 200
    # but a second hit on A is throttled
    assert client.post(f"/api/v1/webhooks/website/{a}", json=body).status_code == 429


def test_website_empty_message_rejected(auth_client):
    client, _token, _uid = auth_client
    integration_id = _website_integration(client)
    r = client.post(
        f"/api/v1/webhooks/website/{integration_id}",
        json={"session_id": "s", "message": ""},
    )
    assert r.status_code == 422  # schema validation (min_length=1)
