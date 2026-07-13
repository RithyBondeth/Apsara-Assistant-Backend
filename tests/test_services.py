"""Unit tests for pure service logic (no DB, no network)."""
from __future__ import annotations

import hashlib
import hmac

from app.services import cloudinary_service, messenger, telegram

# ── Telegram ─────────────────────────────────────────────────────────────────

def test_telegram_parse_text_message():
    update = {"message": {"chat": {"id": 42, "first_name": "Sok", "last_name": "Dara"}, "text": "hi"}}
    msg = telegram.parse_update(update)
    assert msg is not None
    assert msg.external_user_id == "42"
    assert msg.sender_name == "Sok Dara"
    assert msg.text == "hi"


def test_telegram_parse_username_fallback():
    update = {"message": {"chat": {"id": 7, "username": "sokdara"}, "text": "yo"}}
    msg = telegram.parse_update(update)
    assert msg.sender_name == "sokdara"


def test_telegram_parse_ignores_non_text():
    assert telegram.parse_update({"edited_message": {}}) is None
    assert telegram.parse_update({"message": {"chat": {"id": 1}}}) is None  # no text


# ── Messenger ────────────────────────────────────────────────────────────────

def test_messenger_verify_signature_valid_and_invalid():
    secret = "app-secret"
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert messenger.verify_signature(secret, body, good) is True
    assert messenger.verify_signature(secret, body, "sha256=deadbeef") is False
    assert messenger.verify_signature(secret, body, None) is False
    assert messenger.verify_signature("", body, good) is False  # no secret → fail closed


def test_messenger_parse_updates_skips_echo_and_attachments():
    payload = {
        "entry": [
            {
                "messaging": [
                    {"sender": {"id": "1"}, "message": {"text": "hello"}},
                    {"sender": {"id": "2"}, "message": {"text": "echo", "is_echo": True}},
                    {"sender": {"id": "3"}, "message": {"attachments": [{}]}},
                ]
            }
        ]
    }
    msgs = messenger.parse_updates(payload)
    assert len(msgs) == 1
    assert msgs[0].external_user_id == "1"
    assert msgs[0].text == "hello"


# ── Cloudinary ───────────────────────────────────────────────────────────────

def test_cloudinary_signature_matches_documented_algorithm(monkeypatch):
    monkeypatch.setattr(cloudinary_service.settings, "CLOUDINARY_API_SECRET", "abcd")
    params = {"timestamp": "1315060510", "public_id": "sample_image"}
    expected = hashlib.sha1(
        b"public_id=sample_image&timestamp=1315060510abcd"
    ).hexdigest()
    assert cloudinary_service._sign(params) == expected


def test_cloudinary_is_configured(monkeypatch):
    monkeypatch.setattr(cloudinary_service.settings, "CLOUDINARY_CLOUD_NAME", "")
    assert cloudinary_service.is_configured() is False
    monkeypatch.setattr(cloudinary_service.settings, "CLOUDINARY_CLOUD_NAME", "c")
    monkeypatch.setattr(cloudinary_service.settings, "CLOUDINARY_API_KEY", "k")
    monkeypatch.setattr(cloudinary_service.settings, "CLOUDINARY_API_SECRET", "s")
    assert cloudinary_service.is_configured() is True
