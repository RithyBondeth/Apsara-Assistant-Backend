"""Bring inbound customer media onto storage we control.

Re-hosting is required, not a convenience:

* Telegram's download URL embeds the bot token
  (``api.telegram.org/file/bot<TOKEN>/<path>``). Persisting it would write the
  seller's bot credential into the messages table and hand it to anyone who
  can see the conversation — including the browser.
* Meta's CDN links for attachments are short-lived, so a stored one would rot
  and the seller would later open a thread with a broken image.

Both problems disappear once the bytes are copied to Cloudinary and only that
URL is stored.
"""
from __future__ import annotations

import logging

from app.services import cloudinary_service, instagram, messenger, telegram

logger = logging.getLogger(__name__)

# platform -> service module exposing `download_image(access_token, ref)`.
# Module (not function) so tests can monkeypatch and have it resolve at call time.
_DOWNLOADERS = {
    "telegram": telegram,
    "messenger": messenger,
    "instagram": instagram,
}

# Mirrors the seller-facing upload endpoint's cap. An inbound image the AI
# can't afford to process is better dropped than allowed to blow up memory or
# the vision bill.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


# Whisper rejects anything over 25MB and a genuine voice note is orders of
# magnitude smaller; this is a floor against forwarded files, not a quality
# knob.
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class MediaError(Exception):
    """Inbound media could not be fetched or stored."""


async def store_inbound_image(platform: str, access_token: str, image_ref: str) -> str:
    """Fetch a customer's image from its platform and return a hosted URL.

    Raises MediaError; callers treat an image they can't store as a text-only
    message rather than dropping the customer's message entirely.
    """
    service = _DOWNLOADERS.get(platform)
    if service is None:
        raise MediaError(f"No image download support for {platform}")

    if not cloudinary_service.is_configured():
        raise MediaError("Cloudinary is not configured — cannot store inbound images")

    try:
        content, filename = await service.download_image(access_token, image_ref)
    except Exception as exc:
        raise MediaError(f"Could not download the {platform} image") from exc

    if len(content) > MAX_IMAGE_BYTES:
        raise MediaError(
            f"Image is {len(content)} bytes, over the {MAX_IMAGE_BYTES} limit"
        )

    try:
        result = await cloudinary_service.upload_image(
            content, filename, folder="apsara/inbound"
        )
    except Exception as exc:
        raise MediaError("Could not store the image") from exc

    return result["url"]


async def fetch_inbound_voice(
    platform: str, access_token: str, voice_ref: str
) -> tuple[bytes, str]:
    """Fetch a customer's voice note and return ``(bytes, filename)``.

    Unlike images, the bytes are returned rather than stored: transcription
    needs them in memory anyway, and whether the clip is worth keeping depends
    on whether it transcribed cleanly. Callers hand the bytes to
    ``store_inbound_voice`` once they've decided.
    """
    service = _DOWNLOADERS.get(platform)
    if service is None or not hasattr(service, "download_voice"):
        raise MediaError(f"No voice download support for {platform}")

    try:
        content, filename = await service.download_voice(access_token, voice_ref)
    except Exception as exc:
        raise MediaError(f"Could not download the {platform} voice note") from exc

    if len(content) > MAX_AUDIO_BYTES:
        raise MediaError(
            f"Voice note is {len(content)} bytes, over the {MAX_AUDIO_BYTES} limit"
        )

    return content, filename


async def store_inbound_voice(content: bytes, filename: str) -> str:
    """Copy already-fetched voice bytes onto our storage and return the URL.

    Kept even when transcription succeeds: the transcript is a lossy guess at
    what the customer said, and a seller reviewing a thread needs to be able to
    press play and hear the original.
    """
    if not cloudinary_service.is_configured():
        raise MediaError("Cloudinary is not configured — cannot store voice notes")

    try:
        result = await cloudinary_service.upload_image(
            content,
            filename,
            folder="apsara/inbound-voice",
            resource_type=cloudinary_service.AUDIO_RESOURCE_TYPE,
        )
    except Exception as exc:
        raise MediaError("Could not store the voice note") from exc

    return result["url"]
