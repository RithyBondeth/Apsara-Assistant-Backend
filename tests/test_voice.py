"""A customer sends a voice note instead of typing.

The behaviour under test is mostly about restraint: Khmer transcription is
unreliable enough that the interesting cases are the ones where the bot
declines to answer.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

import app.services.media as media_service
import app.services.telegram as telegram_service
import app.services.transcription as transcription
from app.services.telegram import parse_update
from app.services.transcription import Transcript, product_vocabulary_prompt


@dataclass
class FakeSegment:
    """Stands in for a Whisper verbose_json segment."""

    start: float
    end: float
    avg_logprob: float
    no_speech_prob: float = 0.0


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_telegram_voice_note_is_parsed():
    inbound = parse_update(
        {
            "update_id": 11,
            "message": {
                "chat": {"id": 42, "first_name": "Dara"},
                "voice": {"file_id": "voice-abc", "duration": 7},
            },
        }
    )
    assert inbound is not None
    assert inbound.has_voice
    assert inbound.voice_ref == "voice-abc"
    assert inbound.voice_duration == 7
    assert inbound.text == ""


def test_forwarded_audio_file_is_not_treated_as_a_voice_note():
    """`audio` is a music file someone forwarded, not a customer talking."""
    update = {
        "update_id": 12,
        "message": {"chat": {"id": 42}, "audio": {"file_id": "song"}},
    }
    assert parse_update(update) is None


def test_voice_note_with_caption_keeps_both():
    inbound = parse_update(
        {
            "update_id": 13,
            "message": {
                "chat": {"id": 42},
                "voice": {"file_id": "v1", "duration": 3},
                "caption": "សួស្តី",
            },
        }
    )
    assert inbound is not None
    assert inbound.text == "សួស្តី"
    assert inbound.voice_ref == "v1"


# ── Confidence scoring ────────────────────────────────────────────────────────


def test_clean_audio_scores_high_and_is_reliable():
    confidence, no_speech = transcription._confidence_from_segments(
        [FakeSegment(0.0, 2.0, -0.12), FakeSegment(2.0, 4.0, -0.15)]
    )
    assert confidence > 0.8
    assert no_speech == 0.0

    t = Transcript("តម្លៃប៉ុន្មាន", "km", 4.0, confidence, no_speech)
    assert t.is_reliable(threshold=0.55)


def test_model_guessing_scores_low_and_is_not_reliable():
    """A low avg_logprob is Whisper telling us it invented most of this."""
    confidence, _ = transcription._confidence_from_segments(
        [FakeSegment(0.0, 3.0, -1.4)]
    )
    assert confidence < 0.35

    t = Transcript("something plausible", "km", 3.0, confidence, 0.0)
    assert not t.is_reliable(threshold=0.55)


def test_silence_is_not_reliable_even_when_confidently_transcribed():
    """High no_speech_prob means Whisper hallucinated text from noise."""
    t = Transcript("thank you for watching", "km", 2.0, confidence=0.9, no_speech_prob=0.8)
    assert not t.is_reliable(threshold=0.55)


def test_empty_transcript_is_never_reliable():
    assert not Transcript("   ", "km", 1.0, confidence=0.99, no_speech_prob=0.0).is_reliable()


def test_confidence_is_duration_weighted():
    """A long clean sentence shouldn't be dragged down by a short 'um'."""
    mostly_good, _ = transcription._confidence_from_segments(
        [FakeSegment(0.0, 10.0, -0.1), FakeSegment(10.0, 10.3, -2.0)]
    )
    evenly_split, _ = transcription._confidence_from_segments(
        [FakeSegment(0.0, 5.0, -0.1), FakeSegment(5.0, 10.0, -2.0)]
    )
    assert mostly_good > evenly_split


def test_missing_segments_score_zero_rather_than_inventing_confidence():
    confidence, no_speech = transcription._confidence_from_segments([])
    assert confidence == 0.0
    assert no_speech == 1.0


def test_zero_length_segments_do_not_divide_by_zero():
    confidence, _ = transcription._confidence_from_segments(
        [FakeSegment(1.0, 1.0, -0.2)]
    )
    assert 0.0 < confidence <= 1.0


# ── Vocabulary hint ───────────────────────────────────────────────────────────


def test_vocabulary_prompt_prefers_short_names_within_the_cap():
    names = ["Krama Scarf", "A" * 200, "Silk Bag"]
    prompt = product_vocabulary_prompt(names, limit=2)
    assert "Silk Bag" in prompt
    assert "Krama Scarf" in prompt
    assert "A" * 200 not in prompt


def test_vocabulary_prompt_is_empty_without_products():
    assert product_vocabulary_prompt([]) == ""
    assert product_vocabulary_prompt(["", "  "]) == ""


def test_vocabulary_prompt_deduplicates():
    assert product_vocabulary_prompt(["Scarf", "Scarf"]) == "Scarf"


# ── Transcription guards ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_audio_is_rejected_before_paying_for_a_call():
    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"")


@pytest.mark.asyncio
async def test_oversized_audio_is_rejected():
    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"x" * (transcription.MAX_AUDIO_BYTES + 1))


# ── Media fetch ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_download_keeps_the_ogg_extension(monkeypatch):
    """Whisper infers the container from the filename; .jpg would be rejected."""
    captured = {}

    async def fake_download(access_token, file_id, fallback_ext):
        captured["ext"] = fallback_ext
        return b"audio", f"{file_id}{fallback_ext}"

    monkeypatch.setattr(telegram_service, "_download_file", fake_download)
    content, filename = await telegram_service.download_voice("tok", "v1")
    assert captured["ext"] == ".ogg"
    assert filename.endswith(".ogg")
    assert content == b"audio"


@pytest.mark.asyncio
async def test_platform_without_voice_support_raises_media_error():
    with pytest.raises(media_service.MediaError):
        await media_service.fetch_inbound_voice("website", "tok", "v1")


@pytest.mark.asyncio
async def test_oversized_voice_note_is_rejected_after_download(monkeypatch):
    async def fake_download_voice(access_token, ref):
        return b"x" * (media_service.MAX_AUDIO_BYTES + 1), "big.ogg"

    monkeypatch.setattr(telegram_service, "download_voice", fake_download_voice)
    with pytest.raises(media_service.MediaError):
        await media_service.fetch_inbound_voice("telegram", "tok", "v1")


# ── End-to-end gating ─────────────────────────────────────────────────────────
#
# The product decision under test: a voice note only gets an automatic reply
# when transcription was confident AND the seller opted in. Everything else
# lands on the seller with the conversation flagged.

import app.api.v1.endpoints.webhooks as webhooks
import app.services.chat_service as chat_service
from app.core.config import settings
from app.models.conversation import Conversation


def _telegram_integration(client, token="bot-token", secret="s3cr3t") -> str:
    r = client.post(
        "/api/v1/integrations/",
        json={"platform": "telegram", "access_token": token, "secret_token": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _send_voice(client, integration_id, *, duration=5, update_id=900):
    return client.post(
        f"/api/v1/webhooks/telegram/{integration_id}",
        json={
            "update_id": update_id,
            "message": {
                "chat": {"id": 777, "first_name": "Dara"},
                "voice": {"file_id": "v-1", "duration": duration},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
    )


def _patch_voice_stack(monkeypatch, *, confidence, sent, text="តម្លៃប៉ុន្មាន"):
    """Stub the network edges: download, storage, transcription, and outbound."""

    async def fake_fetch(platform, token, ref):
        return b"audio-bytes", "voice.ogg"

    async def fake_store(content, filename):
        return "https://res.cloudinary.com/demo/video/upload/voice.ogg"

    async def fake_transcribe(audio, filename="voice.ogg", *, language="km", prompt=None):
        return Transcript(text, "km", 5.0, confidence, no_speech_prob=0.0)

    async def fake_ai(messages):
        return "auto reply"

    async def fake_send(access_token, chat_id, text, *, human_agent=False):
        sent.append(text)

    monkeypatch.setattr(webhooks.media, "fetch_inbound_voice", fake_fetch)
    monkeypatch.setattr(webhooks.media, "store_inbound_voice", fake_store)
    monkeypatch.setattr(webhooks.transcription, "transcribe", fake_transcribe)
    monkeypatch.setattr(chat_service, "generate_ai_reply", fake_ai)
    monkeypatch.setattr(telegram_service, "send_message", fake_send)


def test_low_confidence_voice_note_is_flagged_not_answered(auth_client, monkeypatch, db_session):
    """The core safety property: don't answer a question we may have misheard."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent: list[str] = []
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_AUTO_REPLY", True)
    _patch_voice_stack(monkeypatch, confidence=0.20, sent=sent)

    assert _send_voice(client, integration_id).status_code == 200
    assert sent == []  # nothing went back to the customer

    convo = db_session.query(Conversation).one()
    assert convo.needs_attention  # but the seller was told


def test_confident_voice_note_is_answered_when_auto_reply_is_on(
    auth_client, monkeypatch, db_session
):
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent: list[str] = []
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_AUTO_REPLY", True)
    _patch_voice_stack(monkeypatch, confidence=0.92, sent=sent)

    assert _send_voice(client, integration_id).status_code == 200
    assert sent == ["auto reply"]


def test_review_mode_never_auto_replies_however_confident(
    auth_client, monkeypatch, db_session
):
    """VOICE_AUTO_REPLY off is the shipping default — transcribe, don't answer."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    sent: list[str] = []
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_AUTO_REPLY", False)
    _patch_voice_stack(monkeypatch, confidence=0.99, sent=sent)

    assert _send_voice(client, integration_id).status_code == 200
    assert sent == []

    convo = db_session.query(Conversation).one()
    assert convo.needs_attention
    # The transcript still reached the seller — that's the point of review mode.
    assert "តម្លៃប៉ុន្មាន" in convo.messages[0].content


def test_overlong_voice_note_is_not_downloaded_or_transcribed(
    auth_client, monkeypatch, db_session
):
    """Reject on the platform's duration field, before spending money."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    called = []

    async def fake_fetch(platform, token, ref):
        called.append("fetch")
        return b"", "voice.ogg"

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_MAX_SECONDS", 60)
    monkeypatch.setattr(webhooks.media, "fetch_inbound_voice", fake_fetch)

    assert _send_voice(client, integration_id, duration=600).status_code == 200
    assert called == []

    convo = db_session.query(Conversation).one()
    assert convo.needs_attention


def test_transcription_failure_still_reaches_the_seller(
    auth_client, monkeypatch, db_session
):
    """A voice note we can't understand must not vanish silently."""
    client, _token, _uid = auth_client
    integration_id = _telegram_integration(client)

    async def fake_fetch(platform, token, ref):
        return b"audio-bytes", "voice.ogg"

    async def fake_store(content, filename):
        return "https://res.cloudinary.com/demo/video/upload/voice.ogg"

    async def boom(audio, filename="voice.ogg", *, language="km", prompt=None):
        raise transcription.TranscriptionError("whisper is down")

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(webhooks.media, "fetch_inbound_voice", fake_fetch)
    monkeypatch.setattr(webhooks.media, "store_inbound_voice", fake_store)
    monkeypatch.setattr(webhooks.transcription, "transcribe", boom)

    assert _send_voice(client, integration_id).status_code == 200

    convo = db_session.query(Conversation).one()
    assert convo.needs_attention
    # The audio itself was stored, so the seller can still press play.
    audio = [a for m in convo.messages for a in m.attachments if a.file_type == "audio"]
    assert len(audio) == 1
