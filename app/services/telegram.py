from __future__ import annotations

import httpx

from app.services.messaging import InboundMessage

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"


async def _download_file(access_token: str, file_id: str, fallback_ext: str) -> tuple[bytes, str]:
    """Fetch any Telegram file by its file_id.

    Two hops: getFile turns the file_id into a storage path, then the file API
    serves the bytes. Note the download URL embeds the bot token — it must
    never be persisted or returned to a client; callers copy the bytes to our
    own storage instead (services/media.py).
    """
    async with httpx.AsyncClient(timeout=20) as client:
        meta = await client.get(
            TELEGRAM_API.format(token=access_token, method="getFile"),
            params={"file_id": file_id},
        )
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]

        resp = await client.get(TELEGRAM_FILE_API.format(token=access_token, path=file_path))
        resp.raise_for_status()

    # file_path looks like "photos/file_42.jpg" — keep just the leaf.
    filename = file_path.rsplit("/", 1)[-1] or f"{file_id}{fallback_ext}"
    return resp.content, filename


async def download_image(access_token: str, file_id: str) -> tuple[bytes, str]:
    """Fetch a photo the customer sent, by its Telegram file_id."""
    return await _download_file(access_token, file_id, ".jpg")


async def download_voice(access_token: str, file_id: str) -> tuple[bytes, str]:
    """Fetch a voice note the customer sent, by its Telegram file_id.

    Telegram voice notes are OGG/Opus. The extension matters downstream —
    Whisper infers the container format from the filename, so a voice note
    that arrives named ``.jpg`` is rejected as an unsupported format.
    """
    return await _download_file(access_token, file_id, ".ogg")


def parse_update(update: dict) -> InboundMessage | None:
    """Extract a text, photo, and/or voice message from a Telegram update.

    Returns None for updates we don't handle (edited messages, callbacks,
    joins) and for messages carrying none of text, photo, or voice — documents
    are still out of scope.
    """
    message = update.get("message")
    if not message:
        return None

    # Telegram distinguishes `voice` (the hold-to-record button, OGG/Opus) from
    # `audio` (a music file the customer forwarded). Only the former is someone
    # talking to the seller, so only the former is worth transcribing.
    voice = message.get("voice") or {}
    voice_ref = voice.get("file_id")
    voice_duration = voice.get("duration")

    # Telegram sends a photo as an array of the same image at ascending sizes;
    # the last is the largest. Take it: the vision model reads detail, and a
    # thumbnail can lose the product entirely.
    photo_sizes = message.get("photo") or []
    image_ref = photo_sizes[-1].get("file_id") if photo_sizes else None

    # A photo's caption is the customer's actual question ("how much is this?"),
    # so it matters more than the photo for understanding intent.
    text = message.get("text") or message.get("caption") or ""

    if not text and image_ref is None and voice_ref is None:
        return None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None

    name = " ".join(
        part for part in (chat.get("first_name"), chat.get("last_name")) if part
    ) or chat.get("username") or f"Telegram {chat_id}"

    # update_id is unique per bot and resent verbatim on redelivery.
    update_id = update.get("update_id")
    event_id = str(update_id) if update_id is not None else None

    return InboundMessage(
        external_user_id=str(chat_id),
        sender_name=name,
        text=text,
        event_id=event_id,
        image_ref=image_ref,
        voice_ref=voice_ref,
        voice_duration=voice_duration,
    )


async def send_message(
    access_token: str, chat_id: str, text: str, *, human_agent: bool = False
) -> None:
    """Send a text reply back to the customer via the Telegram Bot API.

    ``human_agent`` is accepted and ignored so the three push services share one
    signature and ``outbound`` can dispatch by table lookup. Telegram has no
    messaging window — a bot may reply to anyone who has started it — so there
    is nothing to tag.
    """
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
