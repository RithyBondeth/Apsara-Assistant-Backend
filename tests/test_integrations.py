"""Connecting a page or bot, and how its credentials are held."""

import uuid

import pytest

from unittest import mock

import httpx

from app.core import crypto
from app.services import platforms
from app.models.platform_connection import PlatformConnection
from tests import webhooks as wh


def test_connecting_a_page_returns_the_webhook_url(client, seller):
    integration = wh.connect(client, seller, external_id="page-1")

    assert integration["platform"] == "messenger"
    assert integration["webhook_url"].endswith("/api/v1/webhooks/messenger")
    assert integration["is_active"] and integration["auto_reply"]


def test_telegram_gets_its_own_path_and_secret(client, seller):
    """Telegram puts nothing in the payload naming the bot, so the connection
    is identified by the path and authenticated by the secret."""
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    assert integration["webhook_url"].endswith(f"/webhooks/telegram/{integration['id']}")
    assert integration["webhook_secret"]
    assert len(integration["webhook_secret"]) >= 32


def test_messenger_has_no_per_connection_secret(client, seller):
    """It authenticates with an app-level signature instead."""
    assert wh.connect(client, seller)["webhook_secret"] is None


# ── Credential handling ──────────────────────────────────────────────────────

def test_the_token_never_comes_back_out(client, seller):
    integration = wh.connect(client, seller, token="super-secret-page-token")
    listed = client.get("/api/v1/integrations/", headers=seller.headers).json()

    assert "access_token" not in integration
    assert "super-secret-page-token" not in str(listed)


def test_the_token_is_encrypted_at_rest(client, seller, db):
    wh.connect(client, seller, token="super-secret-page-token")

    stored = db.query(PlatformConnection).one().access_token
    assert stored != "super-secret-page-token"
    assert "super-secret-page-token" not in stored
    assert crypto.decrypt(stored) == "super-secret-page-token"


def test_ciphertext_is_not_reused_across_rows(client, seller):
    """Fernet randomises its IV, so identical tokens must not look identical —
    otherwise a dump reveals which sellers share a credential."""
    a = crypto.encrypt("same-token")
    b = crypto.encrypt("same-token")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same-token"


def test_ciphertext_from_another_key_is_refused(monkeypatch):
    ciphertext = crypto.encrypt("token")
    crypto._fernet.cache_clear()
    monkeypatch.setattr(crypto.settings, "SECRET_KEY", "a-completely-different-key")
    try:
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(ciphertext)
    finally:
        crypto._fernet.cache_clear()


# ── Ownership and uniqueness ─────────────────────────────────────────────────

def test_a_page_cannot_be_claimed_twice(client, seller, other_seller):
    """Otherwise a second seller could attach a page and start receiving
    someone else's customer messages."""
    wh.connect(client, seller, external_id="page-1")

    r = client.post("/api/v1/integrations/",
                    json={"platform": "messenger", "external_id": "page-1",
                          "access_token": "another-token"},
                    headers=other_seller.headers)

    assert r.status_code == 409


def test_the_same_id_on_another_platform_is_fine(client, seller):
    wh.connect(client, seller, platform="messenger", external_id="shared-id")
    r = client.post("/api/v1/integrations/",
                    json={"platform": "telegram", "external_id": "shared-id",
                          "access_token": "bot-token"},
                    headers=seller.headers)
    assert r.status_code == 201


def test_integrations_are_listed_per_seller(client, seller, other_seller):
    wh.connect(client, seller, external_id="page-mine")
    wh.connect(client, other_seller, external_id="page-theirs")

    mine = client.get("/api/v1/integrations/", headers=seller.headers).json()
    assert [i["external_id"] for i in mine] == ["page-mine"]


def test_another_seller_cannot_touch_it(client, seller, other_seller):
    integration = wh.connect(client, seller, external_id="page-1")

    assert client.patch(f"/api/v1/integrations/{integration['id']}",
                        json={"is_active": False},
                        headers=other_seller.headers).status_code == 404
    assert client.delete(f"/api/v1/integrations/{integration['id']}",
                         headers=other_seller.headers).status_code == 404


def test_rotating_the_token_re_encrypts_it(client, seller, db):
    integration = wh.connect(client, seller, token="old-token")

    r = client.patch(f"/api/v1/integrations/{integration['id']}",
                     json={"access_token": "rotated-token"}, headers=seller.headers)

    assert r.status_code == 200
    assert crypto.decrypt(db.query(PlatformConnection).one().access_token) == "rotated-token"


def test_disconnecting_removes_it(client, seller, db):
    integration = wh.connect(client, seller, external_id="page-1")
    assert client.delete(f"/api/v1/integrations/{integration['id']}",
                         headers=seller.headers).status_code == 204
    assert db.query(PlatformConnection).count() == 0


def test_unknown_integration_is_404(client, seller):
    assert client.patch(f"/api/v1/integrations/{uuid.uuid4()}", json={},
                        headers=seller.headers).status_code == 404


def test_authentication_is_required(client):
    assert client.get("/api/v1/integrations/").status_code == 401


@pytest.mark.parametrize("payload", [
    {"platform": "whatsapp", "external_id": "x", "access_token": "t"},
    {"platform": "messenger", "external_id": "", "access_token": "t"},
    {"platform": "messenger", "external_id": "x", "access_token": ""},
    {"platform": "messenger", "external_id": "x"},
])
def test_malformed_connections_are_refused(client, seller, payload):
    r = client.post("/api/v1/integrations/", json=payload, headers=seller.headers)
    assert r.status_code == 422


# ── Checking a connection against the platform ───────────────────────────────

def stub_http(monkeypatch, *, get=None, post=None):
    """Replace the outbound HTTP calls the checks make."""
    def _response(payload, status_code=200):
        request = httpx.Request("GET", "https://example.test")
        return httpx.Response(status_code, json=payload, request=request)

    if get is not None:
        monkeypatch.setattr(platforms.httpx, "get",
                            lambda *a, **k: _response(*get))
    if post is not None:
        monkeypatch.setattr(platforms.httpx, "post",
                            lambda *a, **k: _response(*post))


def test_a_good_telegram_token_reports_the_bot(client, seller, monkeypatch):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    stub_http(monkeypatch, get=({"ok": True, "result": {"username": "apsara_bot"}},))

    r = client.post(f"/api/v1/integrations/{integration['id']}/check",
                    headers=seller.headers)

    assert r.status_code == 200
    assert r.json() == {"ok": True, "detail": "Connected to @apsara_bot."}


def test_a_rejected_telegram_token_says_why(client, seller, monkeypatch):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    stub_http(monkeypatch,
              get=({"ok": False, "description": "Unauthorized"}, 401))

    r = client.post(f"/api/v1/integrations/{integration['id']}/check",
                    headers=seller.headers)

    assert r.json() == {"ok": False, "detail": "Unauthorized"}


def test_a_good_page_token_reports_the_page(client, seller, monkeypatch):
    integration = wh.connect(client, seller, external_id="page-1")
    stub_http(monkeypatch, get=({"id": "page-1", "name": "Sok Silk Shop"},))

    r = client.post(f"/api/v1/integrations/{integration['id']}/check",
                    headers=seller.headers)

    assert r.json() == {"ok": True, "detail": "Connected to Sok Silk Shop."}


def test_a_rejected_page_token_surfaces_facebooks_message(client, seller, monkeypatch):
    integration = wh.connect(client, seller, external_id="page-1")
    stub_http(monkeypatch,
              get=({"error": {"message": "Error validating access token"}}, 400))

    r = client.post(f"/api/v1/integrations/{integration['id']}/check",
                    headers=seller.headers)

    assert r.json()["ok"] is False
    assert "validating access token" in r.json()["detail"]


def test_an_unreachable_platform_does_not_blame_the_token(client, seller, monkeypatch):
    """A network failure says nothing about whether the credential is valid."""
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    def boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(platforms.httpx, "get", boom)

    r = client.post(f"/api/v1/integrations/{integration['id']}/check",
                    headers=seller.headers)

    assert r.json()["ok"] is False
    assert "Could not reach" in r.json()["detail"]


def test_registering_a_telegram_webhook_sends_url_and_secret(client, seller, monkeypatch):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    calls = {}

    def capture(endpoint, **kwargs):
        # Kept apart deliberately: the endpoint and the body both have a "url",
        # and merging them hides which is which.
        calls["endpoint"] = endpoint
        calls["body"] = kwargs.get("json") or {}
        return httpx.Response(200, json={"ok": True},
                              request=httpx.Request("POST", endpoint))

    monkeypatch.setattr(platforms.httpx, "post", capture)

    r = client.post(f"/api/v1/integrations/{integration['id']}/register-webhook",
                    headers=seller.headers)

    assert r.json()["ok"] is True
    assert calls["endpoint"].endswith("/setWebhook")
    body = calls["body"]
    assert body["url"] == integration["webhook_url"], "must register the URL we serve"
    assert integration["id"] in body["url"], "which identifies this connection"
    assert body["secret_token"] == integration["webhook_secret"]
    # Only message updates; edits and the rest are ignored on arrival anyway.
    assert body["allowed_updates"] == ["message"]


def test_telegram_refusing_the_webhook_is_reported(client, seller, monkeypatch):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")
    stub_http(monkeypatch,
              post=({"ok": False, "description": "bad webhook: HTTPS url must be provided"}, 400))

    r = client.post(f"/api/v1/integrations/{integration['id']}/register-webhook",
                    headers=seller.headers)

    assert r.json()["ok"] is False
    assert "HTTPS" in r.json()["detail"]


def test_messenger_has_no_webhook_to_register(client, seller):
    """It is configured once per Meta app, not per page."""
    integration = wh.connect(client, seller, external_id="page-1")

    r = client.post(f"/api/v1/integrations/{integration['id']}/register-webhook",
                    headers=seller.headers)

    assert r.status_code == 400
    assert "Meta app dashboard" in r.json()["detail"]


def test_another_seller_cannot_check_or_register(client, seller, other_seller):
    integration = wh.connect(client, seller, platform="telegram", external_id="bot-1")

    for path in ("check", "register-webhook"):
        assert client.post(f"/api/v1/integrations/{integration['id']}/{path}",
                           headers=other_seller.headers).status_code == 404
