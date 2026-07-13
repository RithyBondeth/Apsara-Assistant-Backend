from __future__ import annotations

from dataclasses import dataclass

import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class InboundMessage:
    """A normalized inbound message extracted from a platform update."""

    external_user_id: str  # platform-side sender id (Telegram chat id)
    sender_name: str
    text: str


def parse_update(update: dict) -> InboundMessage | None:
    """Extract a text message from a Telegram update, or None if not handled.

    Ignores non-message updates (edited messages, callbacks, joins) and
    non-text messages — those are out of scope until Phase 7 (voice/image).
    """
    message = update.get("message")
    if not message or "text" not in message:
        return None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None

    name = " ".join(
        part for part in (chat.get("first_name"), chat.get("last_name")) if part
    ) or chat.get("username") or f"Telegram {chat_id}"

    return InboundMessage(
        external_user_id=str(chat_id),
        sender_name=name,
        text=message["text"],
    )


async def send_message(access_token: str, chat_id: str, text: str) -> None:
    """Send a text reply back to the customer via the Telegram Bot API."""
    url = TELEGRAM_API.format(token=access_token, method="sendMessage")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()


async def set_webhook(access_token: str, webhook_url: str, secret_token: str | None) -> dict:
    """Register ``webhook_url`` with Telegram so updates are delivered to us."""
    url = TELEGRAM_API.format(token=access_token, method="setWebhook")
    payload: dict = {"url": webhook_url}
    if secret_token:
        payload["secret_token"] = secret_token
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
