from __future__ import annotations

import hashlib
import hmac
import logging
from urllib.parse import urlparse

import httpx

from app.services.messaging import InboundMessage

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v19.0/{path}"


def verify_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """Verify the ``X-Hub-Signature-256`` header against the raw request body.

    Facebook signs each webhook POST with an HMAC-SHA256 of the body keyed by
    the app secret. A missing header or secret fails closed.
    """
    if not app_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def first_image_url(message: dict) -> str | None:
    """The URL of the first image attached to a Meta message, if any.

    Meta delivers attachments as ``[{type, payload:{url}}]`` and uses the same
    shape for stickers, files, video and audio — only ``image`` is handled.
    The URL is a short-lived CDN link, so it must be copied to our own storage
    rather than stored (see services/media.py).
    """
    for attachment in message.get("attachments") or []:
        if attachment.get("type") != "image":
            continue
        url = (attachment.get("payload") or {}).get("url")
        if url:
            return url
    return None


def parse_updates(payload: dict) -> list[InboundMessage]:
    """Extract text and image messages from a Messenger webhook payload.

    One payload can batch several entries/messages. Echoes (messages the page
    itself sent) are skipped, as are events carrying neither text nor an image
    — voice and files remain out of scope.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo"):
                continue

            image_ref = first_image_url(message)
            text = message.get("text") or ""
            if not text and image_ref is None:
                continue

            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue
            messages.append(
                InboundMessage(
                    external_user_id=str(sender_id),
                    sender_name=f"Messenger {sender_id}",
                    text=text,
                    # mid is unique per message and resent on redelivery
                    event_id=message.get("mid"),
                    image_ref=image_ref,
                )
            )
    return messages


async def download_image(access_token: str, url: str) -> tuple[bytes, str]:
    """Fetch an image the customer sent from Meta's CDN.

    ``access_token`` is unused — the attachment URL is pre-signed — but kept so
    every channel service shares one downloader signature.
    """
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    filename = urlparse(url).path.rsplit("/", 1)[-1] or "messenger-image.jpg"
    return resp.content, filename


async def send_message(
    access_token: str, recipient_id: str, text: str, *, human_agent: bool = False
) -> None:
    """Send a text reply to the customer via the Messenger Send API.

    Set ``human_agent`` when a seller is answering personally. Meta only allows
    an untagged reply within 24 hours of the customer's last message; the
    HUMAN_AGENT tag is the one intended for a person responding to a query and
    widens that to 7 days. It requires the Human Agent permission on the app,
    so an un-reviewed app will see this rejected — the caller surfaces Meta's
    own reason rather than guessing.

    The AI's auto-replies deliberately stay untagged: they answer immediately,
    so they are always inside the standard window.
    """
    url = GRAPH_API.format(path="me/messages")
    body: dict = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    if human_agent:
        body["messaging_type"] = "MESSAGE_TAG"
        body["tag"] = "HUMAN_AGENT"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, params={"access_token": access_token}, json=body)
        resp.raise_for_status()


async def get_profile_name(access_token: str, psid: str) -> str | None:
    """Fetch a customer's display name from their PSID, or None on failure.

    Best-effort: any error (permissions, deleted user, network) returns None so
    the caller falls back to a placeholder rather than dropping the message.
    """
    url = GRAPH_API.format(path=psid)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                params={"fields": "first_name,last_name", "access_token": access_token},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        logger.warning("Messenger profile lookup failed for psid %s", psid)
        return None

    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p)
    return name or None


async def subscribe_page(access_token: str, page_id: str) -> dict:
    """Subscribe the page to the app so its messages reach our webhook."""
    url = GRAPH_API.format(path=f"{page_id}/subscribed_apps")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            params={
                "access_token": access_token,
                "subscribed_fields": "messages,messaging_postbacks",
            },
        )
        resp.raise_for_status()
        return resp.json()
