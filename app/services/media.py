"""Bring an inbound customer image onto storage we control.

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


class MediaError(Exception):
    """An inbound image could not be fetched or stored."""


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
