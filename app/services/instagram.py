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
from app.services.messenger import parse_updates, verify_signature

__all__ = ["parse_updates", "verify_signature", "send_message", "get_profile_name"]

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.instagram.com/v25.0/{path}"


async def send_message(access_token: str, recipient_id: str, text: str) -> None:
    """Send a text reply to an Instagram user via the Instagram Send API."""
    url = GRAPH_API.format(path="me/messages")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            params={"access_token": access_token},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
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
