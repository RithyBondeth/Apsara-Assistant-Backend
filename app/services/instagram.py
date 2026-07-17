"""Instagram Messaging adapter.

Instagram DMs run on Meta's platform, so the inbound webhook format and
signature scheme are identical to Messenger — those helpers are reused. Only
the outbound Send API host (graph.instagram.com) and the profile-name fields
differ, so just those live here.
"""
from __future__ import annotations

import logging

import httpx

# Inbound webhook shape + HMAC signature are the same as Messenger.
# IG DMs ride on Meta's platform: identical webhook payload and signature
# scheme, and attachments use the same {type, payload:{url}} shape — so the
# parser and the CDN downloader are shared verbatim rather than duplicated.
from app.services.messenger import download_image, parse_updates, verify_signature

__all__ = [
    "parse_updates",
    "verify_signature",
    "download_image",
    "send_message",
    "get_profile_name",
]

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.instagram.com/v25.0/{path}"


async def send_message(
    access_token: str, recipient_id: str, text: str, *, human_agent: bool = False
) -> None:
    """Send a text reply to an Instagram user via the Instagram Send API.

    ``human_agent`` carries the same meaning as in the Messenger service: IG
    messaging runs on the same Meta policy, so a seller answering personally
    outside the 24-hour window needs the HUMAN_AGENT tag (7 days).
    """
    url = GRAPH_API.format(path="me/messages")
    body: dict = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    if human_agent:
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = "HUMAN_AGENT"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, params={"access_token": access_token}, json=body)
        resp.raise_for_status()


async def get_profile_name(access_token: str, igsid: str) -> str | None:
    """Fetch an Instagram user's display name from their IGSID, or None.

    Best-effort: any error returns None so the caller falls back to a
    placeholder rather than dropping the message.
    """
    url = GRAPH_API.format(path=igsid)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url, params={"fields": "name,username", "access_token": access_token}
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        logger.warning("Instagram profile lookup failed for igsid %s", igsid)
        return None

    return data.get("name") or data.get("username") or None
