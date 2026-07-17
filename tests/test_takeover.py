"""Human takeover: seller replies go out to the platform, and pausing the AI
stops it auto-replying without losing the customer's messages.
"""
from __future__ import annotations

import httpx

import app.services.chat_service as chat_service
import app.services.instagram as instagram_service
import app.services.messenger as messenger_service
import app.services.telegram as telegram_service


def _telegram_integration(client, token="bot-token", secret="s3cr3t") -> str:
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "telegram", "access_token": token, "secret_token": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _inbound(client, integration_id, text="tinh nhom", chat_id=555):
    return client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={"message": {"chat": {"id": chat_id, "first_name": "Dara"}, "text": text}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )


def _patch_ai_and_send(monkeypatch, sent, reply="auto reply"):
    async def fake_ai(messages):
        return reply

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        sent.append((access_token, chat_id, text))

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)


def test_seller_reply_is_delivered_to_the_customer(auth_client, monkeypatch):
    """The whole point: typing in the dashboard reaches the real customer."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)
    sent.clear()  # drop the AI's auto-reply; we care about the seller's

    conv = client.get("/api/v1/conversations/").json()[0]
    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "We have it in stock!"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["sender_type"] == "seller"

    # It actually went out to Telegram, addressed to that customer's chat id.
    assert len(sent) == 1
    assert sent[0][1] == "555"
    assert sent[0][2] == "We have it in stock!"

    msgs = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()
    assert [m["sender_type"] for m in msgs] == ["customer", "assistant", "seller"]


def test_seller_reply_is_not_saved_when_delivery_fails(auth_client, monkeypatch):
    """A reply the customer never got must not sit in the seller's inbox
    looking like it was sent."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)

    async def boom(access_token, chat_id, text, *, human_agent=False):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(telegram_service, "send_message", boom)

    conv = client.get("/api/v1/conversations/").json()[0]
    before = len(client.get(f"/api/v1/conversations/{conv['id']}/messages").json())

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "this will not arrive"},
    )
    assert r.status_code == 502, r.text

    after = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()
    assert len(after) == before
    assert all(m["content"] != "this will not arrive" for m in after)


def test_seller_cannot_fabricate_customer_or_ai_messages(auth_client, monkeypatch):
    """The old dashboard chat injected fake 'customer' messages into real
    threads. The endpoint now only accepts the seller's own voice."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()[0]

    for forged in ("customer", "assistant"):
        r = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"sender_type": forged, "content": "forged"},
        )
        assert r.status_code == 422, f"{forged!r} should be rejected: {r.text}"


def test_pausing_ai_records_inbound_but_sends_no_reply(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id, text="first question")

    conv = client.get("/api/v1/conversations/").json()[0]
    assert conv["ai_enabled"] is True

    r = client.patch(f"/api/v1/conversations/{conv['id']}", json={"ai_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["ai_enabled"] is False

    sent.clear()
    r = _inbound(client, integration_id, text="second question")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "handled": False, "paused": True}

    # Nothing was auto-sent...
    assert sent == []
    # ...but the customer's message is still in the seller's inbox to answer.
    msgs = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()
    assert [m["sender_type"] for m in msgs] == ["customer", "assistant", "customer"]
    assert msgs[-1]["content"] == "second question"


def test_resuming_ai_starts_replying_again(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id, text="one")

    conv = client.get("/api/v1/conversations/").json()[0]
    client.patch(f"/api/v1/conversations/{conv['id']}", json={"ai_enabled": False})
    _inbound(client, integration_id, text="two")

    client.patch(f"/api/v1/conversations/{conv['id']}", json={"ai_enabled": True})
    sent.clear()
    r = _inbound(client, integration_id, text="three")
    assert r.json() == {"ok": True, "handled": True}
    assert len(sent) == 1


def test_reply_routes_through_the_bot_the_customer_messaged(auth_client, monkeypatch):
    """A seller with two Telegram bots must not answer from the wrong one."""
    client, _token, _uid = auth_client
    first = _telegram_integration(client, token="bot-one")
    second = _telegram_integration(client, token="bot-two", secret="s3cr3t")

    sent = []
    _patch_ai_and_send(monkeypatch, sent)

    # The customer messaged the SECOND bot.
    r = client.post(
        f"/api/v1/webhooks/telegram/{second}",
        json={"message": {"chat": {"id": 777, "first_name": "Sopheak"}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    assert r.status_code == 200, r.text
    sent.clear()

    conv = client.get("/api/v1/conversations/").json()[0]
    client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "reply"}
    )

    assert len(sent) == 1
    # ...so the reply must go out via bot-two's token, not the first one's.
    assert sent[0][0] == "bot-two"
    assert first != second


def test_seller_reply_on_website_conversation_is_rejected(auth_client):
    """The widget is request/response — there's no channel to push a human
    reply down, so the seller must not think one was delivered."""
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "website", "access_token": "public-key"},
    )
    assert r.status_code == 201, r.text

    cust = client.post(
        "/api/v1/customers/",
        json={"name": "Visitor", "platform": "website", "platform_id": "sess-1"},
    ).json()
    conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": cust["id"], "platform": "website"},
    ).json()

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hello?"}
    )
    assert r.status_code == 502, r.text
    assert "website" in r.json()["detail"].lower()


def test_seller_reply_needs_a_connected_channel(auth_client, monkeypatch):
    """No active integration means the reply cannot go anywhere."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)
    conv = client.get("/api/v1/conversations/").json()[0]

    # Seller disconnects the bot, then tries to reply from the dashboard.
    assert client.delete(f"/api/v1/integrations/{integration_id}").status_code == 204

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "anyone?"}
    )
    assert r.status_code == 502, r.text


def test_seller_reply_is_tagged_as_human_agent(auth_client, monkeypatch):
    """Meta rejects an untagged reply more than 24h after the customer wrote.

    HUMAN_AGENT is the tag meant for a person answering personally, so the
    takeover path must set it — otherwise a seller replying the next day fails.
    """
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "messenger",
            "access_token": "fb-token",
            "external_id": "page-1",
            "app_secret": "fb-secret",
        },
    )
    assert r.status_code == 201, r.text

    calls = []

    async def fake_send(access_token, recipient_id, text, *, human_agent=False):
        calls.append(human_agent)

    monkeypatch.setattr(messenger_service, "send_message", fake_send)

    cust = client.post(
        "/api/v1/customers/",
        json={"name": "FB User", "platform": "messenger", "platform_id": "psid-5"},
    ).json()
    conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": cust["id"], "platform": "messenger"},
    ).json()

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "late reply"}
    )
    assert r.status_code == 201, r.text
    assert calls == [True]


def test_ai_autoreply_is_not_tagged_as_human_agent(auth_client, monkeypatch):
    """The bot answers instantly, so it's always inside the standard window.
    Tagging it would misrepresent an automated reply as a human one."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    calls = []

    async def fake_ai(messages):
        return "auto"

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        calls.append(human_agent)

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)

    _inbound(client, integration_id)
    assert calls == [False]


def test_platform_rejection_reason_reaches_the_seller(auth_client, monkeypatch):
    """A generic "channel rejected it" hides the one thing the seller can act
    on — Meta's own wording explains the window."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)

    meta_message = (
        "This message is sent outside of allowed window. "
        "Please refer to the Platform docs."
    )

    async def rejecting_send(access_token, chat_id, text, *, human_agent=False):
        request = httpx.Request("POST", "https://example.test/send")
        response = httpx.Response(
            400, json={"error": {"message": meta_message, "code": 10}}, request=request
        )
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(telegram_service, "send_message", rejecting_send)

    conv = client.get("/api/v1/conversations/").json()[0]
    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "too late"}
    )
    assert r.status_code == 502
    assert r.json()["detail"] == meta_message


def test_telegram_rejection_reason_reaches_the_seller(auth_client, monkeypatch):
    """Telegram reports errors in its own shape, not Meta's."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent = []
    _patch_ai_and_send(monkeypatch, sent)
    _inbound(client, integration_id)

    async def rejecting_send(access_token, chat_id, text, *, human_agent=False):
        request = httpx.Request("POST", "https://example.test/send")
        response = httpx.Response(
            403,
            json={"ok": False, "description": "Forbidden: bot was blocked by the user"},
            request=request,
        )
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr(telegram_service, "send_message", rejecting_send)

    conv = client.get("/api/v1/conversations/").json()[0]
    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hello?"}
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "Forbidden: bot was blocked by the user"


def test_instagram_seller_reply_uses_instagram_sender(auth_client, monkeypatch):
    """Dispatch picks the service matching the conversation's platform."""
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "instagram",
            "access_token": "ig-token",
            "app_secret": "ig-secret",
        },
    )
    assert r.status_code == 201, r.text

    sent = []

    async def fake_send(access_token, recipient_id, text, *, human_agent=False):
        sent.append((access_token, recipient_id, text))

    monkeypatch.setattr(instagram_service, "send_message", fake_send)

    cust = client.post(
        "/api/v1/customers/",
        json={"name": "IG User", "platform": "instagram", "platform_id": "igsid-9"},
    ).json()
    conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": cust["id"], "platform": "instagram"},
    ).json()

    r = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "ig reply"}
    )
    assert r.status_code == 201, r.text
    assert sent == [("ig-token", "igsid-9", "ig reply")]
