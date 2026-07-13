"""Secrets-at-rest: integration tokens are stored encrypted."""
from __future__ import annotations

from sqlalchemy import text

from app.core.crypto import decrypt, encrypt


def test_crypto_roundtrip():
    assert decrypt(encrypt("bot-token-123")) == "bot-token-123"


def test_ciphertext_differs_from_plaintext():
    # Fernet is non-deterministic; ciphertext must not equal the plaintext
    ct = encrypt("secret")
    assert ct != "secret"
    assert decrypt(ct) == "secret"


def test_integration_tokens_encrypted_at_rest(auth_client, db_session):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/integrations/",
        json={
            "platform": "telegram",
            "access_token": "super-secret-bot-token",
            "secret_token": "verify-secret",
        },
    )
    assert r.status_code == 201
    # response never leaks the tokens
    assert "access_token" not in r.json()
    assert "secret_token" not in r.json()

    # Raw SQL read bypasses the EncryptedString type → sees ciphertext.
    # (Only one integration exists in this fresh test DB.)
    stored_access, stored_secret = db_session.execute(
        text("SELECT access_token, secret_token FROM platform_integrations")
    ).one()
    assert stored_access != "super-secret-bot-token"
    assert stored_secret != "verify-secret"
    # ...but it decrypts back to the original
    assert decrypt(stored_access) == "super-secret-bot-token"
    assert decrypt(stored_secret) == "verify-secret"
