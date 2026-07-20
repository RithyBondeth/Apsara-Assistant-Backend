"""Phase 7: a customer sends a photo and the AI answers about it."""
from __future__ import annotations

import asyncio

import pytest

import app.services.chat_service as chat_service
import app.services.cloudinary_service as cloudinary_service
import app.services.media as media_service
import app.services.telegram as telegram_service
from app.services.ai_service import build_openai_messages, detect_reply_language
from app.services.messenger import first_image_url, parse_updates
from app.services.telegram import parse_update

HOSTED = "https://res.cloudinary.com/demo/image/upload/scarf.jpg"


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_telegram_photo_is_parsed_with_its_caption():
    """Photos used to be dropped entirely — a customer got silence."""
    inbound = parse_update(
        {
            "update_id": 7,
            "message": {
                "chat": {"id": 42, "first_name": "Dara"},
                "caption": "នេះថ្លៃប៉ុន្មាន?",
                "photo": [
                    {"file_id": "small", "width": 90},
                    {"file_id": "large", "width": 1280},
                ],
            },
        }
    )
    assert inbound is not None
    # The largest size wins: a thumbnail can lose the product entirely.
    assert inbound.image_ref == "large"
    assert inbound.text == "នេះថ្លៃប៉ុន្មាន?"
    assert inbound.has_image


def test_telegram_photo_with_no_caption_is_still_a_message():
    inbound = parse_update(
        {
            "update_id": 8,
            "message": {"chat": {"id": 42}, "photo": [{"file_id": "only"}]},
        }
    )
    assert inbound is not None
    assert inbound.text == ""
    assert inbound.image_ref == "only"


def test_telegram_update_with_neither_text_nor_photo_is_ignored():
    assert parse_update({"message": {"chat": {"id": 1}, "voice": {"file_id": "v"}}}) is None
    assert parse_update({"edited_message": {"text": "hi"}}) is None


def test_messenger_image_attachment_is_parsed():
    msgs = parse_updates(
        {
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "psid-1"},
                            "message": {
                                "mid": "m1",
                                "attachments": [
                                    {
                                        "type": "image",
                                        "payload": {"url": "https://cdn.meta/x.jpg"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
    )
    assert len(msgs) == 1
    assert msgs[0].image_ref == "https://cdn.meta/x.jpg"


def test_non_image_attachments_are_ignored():
    """Stickers/audio/video share the attachment shape but aren't in scope."""
    for kind in ("audio", "video", "file", "fallback"):
        assert first_image_url({"attachments": [{"type": kind, "payload": {"url": "u"}}]}) is None


def test_messenger_echo_is_still_skipped_with_attachments():
    msgs = parse_updates(
        {
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "page"},
                            "message": {
                                "mid": "m2",
                                "is_echo": True,
                                "attachments": [
                                    {"type": "image", "payload": {"url": "u"}}
                                ],
                            },
                        }
                    ]
                }
            ]
        }
    )
    assert msgs == []


# ── Language ──────────────────────────────────────────────────────────────────


class _Msg:
    def __init__(self, sender_type, content, attachments=None):
        self.sender_type = sender_type
        self.content = content
        self.attachments = attachments or []


def test_photo_without_caption_replies_in_the_customers_language():
    """A photo carries no language signal; defaulting a Khmer speaker to
    English on the one message where they typed nothing would be a regression."""
    history = [_Msg("customer", "តើអ្នកមានក្រមាទេ?"), _Msg("assistant", "បាទ")]
    assert detect_reply_language("", history) == "khmer"


def test_photo_language_falls_back_to_english_with_no_history():
    assert detect_reply_language("", []) == "english"


def test_text_still_wins_over_history():
    history = [_Msg("customer", "តើអ្នកមានក្រមាទេ?")]
    assert detect_reply_language("how much is this?", history) == "english"


# ── Payload building ──────────────────────────────────────────────────────────


class _Att:
    def __init__(self, url, file_type="image"):
        self.file_url = url
        self.file_type = file_type


def test_new_image_is_sent_to_the_vision_model():
    msgs = build_openai_messages("sys", [], "how much?", new_image_urls=[HOSTED])
    content = msgs[-1]["content"]
    assert {"type": "text", "text": "how much?"} in content
    assert {"type": "image_url", "image_url": {"url": HOSTED}} in content


def test_text_only_turns_stay_plain_strings():
    """Don't pay the multimodal payload cost for the common case."""
    msgs = build_openai_messages("sys", [], "how much?")
    assert msgs[-1]["content"] == "how much?"


def test_history_photo_survives_a_follow_up_question():
    """Customers send a photo, THEN ask "how much?" — dropping the photo from
    history would leave the model answering about nothing."""
    history = [_Msg("customer", "", attachments=[_Att(HOSTED)])]
    msgs = build_openai_messages("sys", history, "នេះថ្លៃប៉ុន្មាន?")

    photo_turn = msgs[1]["content"]
    assert {"type": "image_url", "image_url": {"url": HOSTED}} in photo_turn


def test_older_images_degrade_to_a_placeholder():
    """Images are billed per tile; only the most recent few are re-sent."""
    history = [
        _Msg("customer", "", attachments=[_Att(f"https://img/{i}.jpg")])
        for i in range(5)
    ]
    msgs = build_openai_messages("sys", history, "and this?")

    sent = [
        part["image_url"]["url"]
        for m in msgs
        if isinstance(m["content"], list)
        for part in m["content"]
        if part.get("type") == "image_url"
    ]
    # Only the newest two survive...
    assert sent == ["https://img/3.jpg", "https://img/4.jpg"]
    # ...and the dropped ones leave a trace rather than vanishing.
    assert any("[the customer sent a photo]" == m["content"] for m in msgs)


# ── Storage ───────────────────────────────────────────────────────────────────


def test_inbound_image_is_rehosted_not_linked(monkeypatch):
    """Telegram's download URL embeds the bot token, so the platform URL must
    never be what we store."""
    captured = {}

    async def fake_download(access_token, ref):
        captured["token"] = access_token
        captured["ref"] = ref
        return b"jpegbytes", "photo.jpg"

    async def fake_upload(content, filename, folder="x"):
        captured["uploaded"] = (content, filename)
        return {"url": HOSTED, "public_id": "p"}

    monkeypatch.setattr(telegram_service, "download_image", fake_download)
    monkeypatch.setattr(cloudinary_service, "upload_image", fake_upload)
    monkeypatch.setattr(cloudinary_service, "is_configured", lambda: True)

    url = asyncio.run(media_service.store_inbound_image("telegram", "bot-token", "file-1"))

    assert url == HOSTED
    assert captured["uploaded"] == (b"jpegbytes", "photo.jpg")
    assert "bot-token" not in url


def test_oversized_image_is_rejected(monkeypatch):
    async def fake_download(access_token, ref):
        return b"x" * (media_service.MAX_IMAGE_BYTES + 1), "big.jpg"

    monkeypatch.setattr(telegram_service, "download_image", fake_download)
    monkeypatch.setattr(cloudinary_service, "is_configured", lambda: True)

    with pytest.raises(media_service.MediaError):
        asyncio.run(media_service.store_inbound_image("telegram", "t", "f"))


# ── End to end ────────────────────────────────────────────────────────────────


def _telegram_integration(client) -> str:
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "telegram", "access_token": "bot-token", "secret_token": "s3cr3t"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_customer_photo_reaches_the_ai_and_is_stored(auth_client, monkeypatch):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    seen: dict = {}

    async def fake_ai(messages):
        seen["messages"] = messages
        return "Yes — the silk scarf, $24.50"

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        pass

    async def fake_download(access_token, ref):
        return b"bytes", "scarf.jpg"

    async def fake_upload(content, filename, folder="x"):
        return {"url": HOSTED, "public_id": "p"}

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)
    monkeypatch.setattr(telegram_service, "download_image", fake_download)
    monkeypatch.setattr(cloudinary_service, "upload_image", fake_upload)
    monkeypatch.setattr(cloudinary_service, "is_configured", lambda: True)

    r = client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={
            "update_id": 1,
            "message": {
                "chat": {"id": 555, "first_name": "Dara"},
                "caption": "how much?",
                "photo": [{"file_id": "big"}],
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "handled": True}

    # The vision model actually received the image...
    last = seen["messages"][-1]["content"]
    assert {"type": "image_url", "image_url": {"url": HOSTED}} in last

    # ...and the seller's inbox shows it, hosted by us, not by Telegram.
    conv = client.get("/api/v1/conversations/").json()["items"][0]
    msgs = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()["items"]
    photo = msgs[0]
    assert photo["message_type"] == "image"
    assert len(photo["attachments"]) == 1
    assert photo["attachments"][0]["file_url"] == HOSTED


def test_a_photo_that_cannot_be_stored_still_delivers_the_message(auth_client, monkeypatch):
    """Losing the photo is bad; silently dropping the customer's message is worse."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    async def fake_ai(messages):
        return "reply"

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        pass

    async def broken_download(access_token, ref):
        raise RuntimeError("telegram getFile failed")

    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)
    monkeypatch.setattr(telegram_service, "download_image", broken_download)
    monkeypatch.setattr(cloudinary_service, "is_configured", lambda: True)

    r = client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={
            "update_id": 2,
            "message": {
                "chat": {"id": 5},
                "caption": "this one?",
                "photo": [{"file_id": "x"}],
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )
    assert r.status_code == 200
    assert r.json()["handled"] is True

    conv = client.get("/api/v1/conversations/").json()["items"][0]
    msgs = client.get(f"/api/v1/conversations/{conv['id']}/messages").json()["items"]
    assert msgs[0]["content"] == "this one?"
    assert msgs[0]["attachments"] == []
