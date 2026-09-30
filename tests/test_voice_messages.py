"""Messages the assistant cannot read still reach the seller.

A voice note, a sticker or a video used to hit the webhook and vanish — not
stored, not answered, not mentioned. To the customer that is being ignored.
"""

from unittest import mock

import pytest

from app.models.message import Message
from app.services.platforms import (
    FILE, OTHER, STICKER, TEXT, VIDEO, VOICE, AttachmentDownload,
    parse_messenger_entry, parse_telegram_update,
)
from tests import ai, webhooks as wh
from tests.test_seller_alerts import alerts, link, the_conversation


def telegram(update_id=1, chat_id=555, **message):
    return {"update_id": update_id, "message": {
        "message_id": 10, "from": {"id": chat_id, "is_bot": False, "first_name": "Srey"},
        "chat": {"id": chat_id, "type": "private"}, "date": 1, **message,
    }}


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_a_telegram_voice_note_is_a_voice_message_with_the_audio_attached():
    parsed = parse_telegram_update(telegram(voice={
        "file_id": "voice-1", "duration": 4, "mime_type": "audio/ogg", "file_size": 9000,
    }))
    assert parsed is not None
    assert parsed.kind == VOICE
    assert parsed.text is None
    assert parsed.attachments[0].platform_file_id == "voice-1"
    assert parsed.attachments[0].file_type == "audio/ogg"


@pytest.mark.parametrize("message, kind", [
    ({"video": {"file_id": "v", "duration": 3}}, VIDEO),
    ({"video_note": {"file_id": "vn"}}, VIDEO),
    ({"sticker": {"file_id": "s", "emoji": "😂"}}, STICKER),
    ({"document": {"file_id": "d", "file_name": "invoice.pdf", "mime_type": "application/pdf"}}, FILE),
    ({"poll": {"question": "?"}}, OTHER),
])
def test_other_telegram_content_is_kept_with_its_kind(message, kind):
    parsed = parse_telegram_update(telegram(**message))
    assert parsed is not None, "must not be dropped"
    assert parsed.kind == kind


def test_a_shared_location_becomes_text_the_assistant_can_use():
    parsed = parse_telegram_update(telegram(location={"latitude": 11.5564, "longitude": 104.9282}))
    assert parsed.kind == TEXT
    assert parsed.text == "📍 https://maps.google.com/?q=11.5564,104.9282"


def test_a_shared_contact_becomes_text():
    parsed = parse_telegram_update(telegram(contact={
        "phone_number": "+85512345678", "first_name": "Srey", "last_name": "Neang",
    }))
    assert parsed.text == "📞 Srey Neang +85512345678"


def test_a_photo_is_still_plain_text_with_an_image():
    parsed = parse_telegram_update(telegram(photo=[{"file_id": "p", "file_size": 5}]))
    assert parsed.kind == TEXT
    assert parsed.attachments[0].file_type == "image"


def test_messenger_audio_is_a_voice_message_and_video_is_noted():
    _, [audio] = parse_messenger_entry({"id": "page-1", "messaging": [{
        "sender": {"id": "psid-1"}, "message": {"mid": "m1", "attachments": [
            {"type": "audio", "payload": {"url": "https://cdn.fbsbx.com/v/voice.mp4"}},
        ]},
    }]})
    assert audio.kind == VOICE
    assert audio.attachments[0].file_type == "audio"

    _, [video] = parse_messenger_entry({"id": "page-1", "messaging": [{
        "sender": {"id": "psid-1"}, "message": {"mid": "m2", "attachments": [
            {"type": "video", "payload": {"url": "https://cdn.fbsbx.com/v/clip.mp4"}},
        ]},
    }]})
    assert video.kind == VIDEO
    assert video.attachments == (), "videos are noted, not downloaded"


def test_a_messenger_sticker_is_kept():
    _, [sticker] = parse_messenger_entry({"id": "page-1", "messaging": [{
        "sender": {"id": "psid-1"}, "message": {"mid": "m3", "sticker_id": 369239263222822,
            "attachments": [{"type": "image", "payload": {"url": "https://cdn.fbsbx.com/s.png", "sticker_id": 1}}]},
    }]})
    assert sticker.kind == STICKER


# ── End to end ───────────────────────────────────────────────────────────────

def test_a_voice_note_is_stored_playable_and_wakes_the_seller(client, seller, db):
    integration = link(client, seller, db)
    audio = AttachmentDownload(b"OggS...", "audio/ogg", "telegram-7.ogg")

    with mock.patch("app.services.inbound.download_attachment", return_value=audio), \
            wh.sends() as to_customer, alerts() as to_seller, ai.replies() as captured:
        r = wh.post_telegram(client, integration["id"],
                             telegram(update_id=7, voice={"file_id": "voice-1", "mime_type": "audio/ogg"}),
                             secret=integration["webhook_secret"])

    assert r.status_code == 200
    assert captured == {}, "the assistant is not asked to answer audio"
    assert to_customer == []

    conversation = the_conversation(client, seller)
    assert conversation["last_message_type"] == "voice"
    assert conversation["needs_attention_at"] is not None
    [alert] = to_seller
    assert "🎤 Srey sent a voice message" in alert["text"]

    [message] = client.get(f"/api/v1/conversations/{conversation['id']}/messages",
                           headers=seller.headers).json()
    assert message["message_type"] == "voice"
    assert message["content"] is None
    [attachment] = message["attachments"]
    assert attachment["file_type"] == "audio/ogg"
    # The seller can play it back from the app.
    played = client.get(f"/api/v1/attachments/{attachment['id']}/content", headers=seller.headers)
    assert played.status_code == 200
    assert played.headers["content-type"] == "audio/ogg"
    assert played.content == b"OggS..."


def test_a_voice_note_that_cannot_be_downloaded_still_reaches_the_seller(client, seller, db):
    integration = link(client, seller, db)
    with mock.patch("app.services.inbound.download_attachment", side_effect=ValueError("gone")), \
            wh.sends(), alerts() as to_seller, ai.replies():
        wh.post_telegram(client, integration["id"],
                         telegram(update_id=8, voice={"file_id": "voice-2"}),
                         secret=integration["webhook_secret"])

    assert "voice message" in to_seller[0]["text"]
    [message] = db.query(Message).all()
    assert message.message_type == "voice"
    assert message.attachments == []


@pytest.mark.parametrize("message, message_type", [
    ({"sticker": {"file_id": "s"}}, "sticker"),
    ({"video": {"file_id": "v"}}, "video"),
])
def test_stickers_and_videos_are_stored_and_the_seller_is_told(client, seller, db, message, message_type):
    integration = link(client, seller, db)
    with wh.sends(), alerts() as to_seller, ai.replies() as captured:
        wh.post_telegram(client, integration["id"], telegram(update_id=9, **message),
                         secret=integration["webhook_secret"])

    assert captured == {}
    assert "📎 Srey sent a video, sticker or file" in to_seller[0]["text"]
    assert the_conversation(client, seller)["last_message_type"] == message_type


def test_a_voice_note_with_a_caption_is_answered_and_still_flagged(client, seller, db):
    """Telegram audio (not voice) can carry a caption; the text is answered,
    the audio is not, and the seller hears about the part that was not."""
    integration = link(client, seller, db)
    audio = AttachmentDownload(b"ID3...", "audio/mpeg", "song.mp3")
    with mock.patch("app.services.inbound.download_attachment", return_value=audio), \
            wh.sends() as to_customer, alerts() as to_seller, ai.replies("Bat!"):
        wh.post_telegram(client, integration["id"],
                         telegram(update_id=10, caption="is this one in stock?",
                                  audio={"file_id": "a-1", "mime_type": "audio/mpeg"}),
                         secret=integration["webhook_secret"])

    assert to_customer[0]["text"] == "Bat!"
    assert "voice message" in to_seller[0]["text"]


def test_alerts_in_khmer(client, seller, db):
    integration = link(client, seller, db, language="km")
    with wh.sends(), alerts() as to_seller, ai.replies():
        wh.post_telegram(client, integration["id"], telegram(update_id=11, sticker={"file_id": "s"}),
                         secret=integration["webhook_secret"])
    assert "ស្ទីកឃ័រ" in to_seller[0]["text"]
