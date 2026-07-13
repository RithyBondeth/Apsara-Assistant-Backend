from __future__ import annotations

import hashlib
import hmac

import httpx

from app.services.messaging import InboundMessage

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


def parse_updates(payload: dict) -> list[InboundMessage]:
    """Extract text messages from a Messenger webhook payload.

    One payload can batch several entries/messages. Echoes (messages the page
    itself sent) and non-text events are skipped — attachments are Phase 7.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo") or "text" not in message:
                continue
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue
            messages.append(
                InboundMessage(
                    external_user_id=str(sender_id),
                    sender_name=f"Messenger {sender_id}",
                    text=message["text"],
                )
            )
    return messages


async def send_message(access_token: str, recipient_id: str, text: str) -> None:
    """Send a text reply to the customer via the Messenger Send API."""
    url = GRAPH_API.format(path="me/messages")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            params={"access_token": access_token},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        resp.raise_for_status()


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
