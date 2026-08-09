"""Talking to Messenger and Telegram.

Each platform contributes three things: a way to prove a webhook really came
from it, a way to read an inbound text message out of its payload, and a way to
send a reply back.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.crypto import decrypt

logger = logging.getLogger(__name__)

MESSENGER = "messenger"
TELEGRAM = "telegram"

SEND_TIMEOUT = 10.0


@dataclass(frozen=True)
class InboundMessage:
    """One customer message, normalised across platforms."""

    external_id: str
    sender_id: str
    text: str
    sender_name: str | None = None


# ── Authenticating the caller ────────────────────────────────────────────────

def verify_messenger_signature(raw_body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 over the exact bytes received.

    Computed on the raw body, never on a re-serialised dict: any difference in
    key order or spacing changes the digest and would reject genuine traffic.
    """
    if not header or not settings.META_APP_SECRET:
        return False
    algorithm, _, provided = header.partition("=")
    if algorithm != "sha256" or not provided:
        return False

    expected = hmac.new(
        settings.META_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    # Constant-time: a plain == leaks how much of the digest matched.
    return hmac.compare_digest(expected, provided)


def verify_telegram_secret(expected: str | None, header: str | None) -> bool:
    if not expected or not header:
        return False
    return hmac.compare_digest(expected, header)


# ── Reading inbound payloads ─────────────────────────────────────────────────

def parse_messenger_entry(entry: dict) -> tuple[str | None, list[InboundMessage]]:
    """Pull customer messages out of one webhook entry.

    Returns the page id alongside them, since that is what identifies the
    seller. Everything that is not a customer's text is dropped: delivery and
    read receipts, postbacks, attachments without a caption, and — most
    importantly — echoes, which are the page's own outgoing messages fed back.
    Treating an echo as input would have the assistant answer itself in a loop.
    """
    page_id = entry.get("id")
    messages: list[InboundMessage] = []

    for event in entry.get("messaging") or []:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("is_echo"):
            continue

        text = message.get("text")
        sender_id = (event.get("sender") or {}).get("id")
        external_id = message.get("mid")
        if not (text and sender_id and external_id):
            continue

        messages.append(
            InboundMessage(external_id=external_id, sender_id=str(sender_id), text=text)
        )

    return page_id, messages


def parse_telegram_update(update: dict) -> InboundMessage | None:
    """Pull a customer message out of one Telegram update.

    Only fresh incoming text is handled. Edits arrive as `edited_message` and
    are ignored rather than answered a second time, and messages from other
    bots are skipped.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    sender = message.get("from") or {}
    chat_id = (message.get("chat") or {}).get("id")
    update_id = update.get("update_id")

    if not text or sender.get("is_bot") or chat_id is None or update_id is None:
        return None

    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ) or sender.get("username")

    return InboundMessage(
        external_id=str(update_id),
        sender_id=str(chat_id),
        text=text,
        sender_name=name or None,
    )


# ── Checking a connection actually works ─────────────────────────────────────

@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


def check_credentials(platform: str, encrypted_token: str) -> CheckResult:
    """Ask the platform whether this token is any good.

    The webhook path cannot tell a bad token from a quiet page — both look like
    silence. This asks directly, so a seller finds out at the moment they
    connect rather than the first time a customer is left unanswered.
    """
    try:
        token = decrypt(encrypted_token)
    except Exception:
        return CheckResult(False, "Stored credential could not be read. Reconnect "
                                  "this channel to store it again.")

    try:
        if platform == TELEGRAM:
            response = httpx.get(f"https://api.telegram.org/bot{token}/getMe",
                                 timeout=SEND_TIMEOUT)
            body = response.json()
            if response.status_code >= 400 or not body.get("ok"):
                return CheckResult(False, body.get("description")
                                   or f"Telegram refused the token ({response.status_code}).")
            bot = body.get("result", {})
            handle = bot.get("username") or bot.get("first_name") or "bot"
            return CheckResult(True, f"Connected to @{handle}.")

        if platform == MESSENGER:
            response = httpx.get(
                f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}/me",
                params={"fields": "id,name", "access_token": token},
                timeout=SEND_TIMEOUT,
            )
            body = response.json()
            if response.status_code >= 400:
                message = (body.get("error") or {}).get("message")
                return CheckResult(False, message
                                   or f"Facebook refused the token ({response.status_code}).")
            return CheckResult(True, f"Connected to {body.get('name') or body.get('id')}.")

        return CheckResult(False, f"Unsupported platform {platform!r}.")
    except httpx.HTTPError as exc:
        # A network failure says nothing about the token, so do not imply it.
        logger.warning("Could not reach %s to check credentials: %s", platform, exc)
        return CheckResult(False, f"Could not reach {platform}. Try again shortly.")
    except Exception:
        logger.exception("Credential check failed for %s", platform)
        return CheckResult(False, "The check failed unexpectedly.")


def register_telegram_webhook(encrypted_token: str, url: str, secret: str) -> CheckResult:
    """Point a bot at our webhook, so the seller never has to call setWebhook.

    Telegram requires HTTPS and refuses a localhost URL, which is why this
    fails plainly during local development rather than appearing to work.
    """
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{decrypt(encrypted_token)}/setWebhook",
            json={"url": url, "secret_token": secret,
                  "allowed_updates": ["message"]},
            timeout=SEND_TIMEOUT,
        )
        body = response.json()
        if response.status_code >= 400 or not body.get("ok"):
            return CheckResult(False, body.get("description")
                               or f"Telegram refused the webhook ({response.status_code}).")
        return CheckResult(True, f"Telegram will now deliver updates to {url}")
    except httpx.HTTPError as exc:
        logger.warning("Could not reach Telegram to register a webhook: %s", exc)
        return CheckResult(False, "Could not reach Telegram. Try again shortly.")
    except Exception:
        logger.exception("setWebhook failed")
        return CheckResult(False, "Registering the webhook failed unexpectedly.")


# ── Looking up who is writing ────────────────────────────────────────────────

def fetch_messenger_profile(encrypted_token: str, psid: str) -> str | None:
    """Ask Graph for a sender's name, or None.

    Messenger identifies a customer only by an opaque page-scoped id, so
    without this every conversation is headed "Customer 123456". The lookup
    needs pages_user_locale / pages_messaging on a page the user has messaged,
    and returns nothing for a customer who has not granted it — so the caller
    must have a fallback rather than treating this as reliable.
    """
    try:
        response = httpx.get(
            f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}/{psid}",
            params={"fields": "first_name,last_name",
                    "access_token": decrypt(encrypted_token)},
            timeout=SEND_TIMEOUT,
        )
        if response.status_code >= 400:
            logger.info("No Messenger profile for %s: %s", psid, response.text[:200])
            return None
        data = response.json()
        name = " ".join(
            part for part in (data.get("first_name"), data.get("last_name")) if part
        ).strip()
        return name or None
    except Exception:
        # A name is a nicety; failing to get one must not cost the message.
        logger.exception("Messenger profile lookup failed for %s", psid)
        return None


# ── Sending replies ──────────────────────────────────────────────────────────

def send_reply(platform: str, encrypted_token: str, recipient_id: str, text: str) -> bool:
    """Deliver a reply. Returns whether the platform accepted it.

    Never raises: this runs after the webhook has already been acknowledged, so
    there is no caller left to handle an exception meaningfully.
    """
    try:
        token = decrypt(encrypted_token)
        if platform == MESSENGER:
            url = f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}/me/messages"
            response = httpx.post(
                url,
                params={"access_token": token},
                json={"recipient": {"id": recipient_id},
                      "messaging_type": "RESPONSE",
                      "message": {"text": text}},
                timeout=SEND_TIMEOUT,
            )
        elif platform == TELEGRAM:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": recipient_id, "text": text},
                timeout=SEND_TIMEOUT,
            )
        else:
            logger.error("No sender for platform %r", platform)
            return False

        if response.status_code >= 400:
            # Body, not just status: the platforms explain refusals there
            # (expired token, outside the messaging window).
            logger.error("%s rejected a reply: %s %s",
                         platform, response.status_code, response.text[:500])
            return False
        return True
    except Exception:
        logger.exception("Failed to send a %s reply", platform)
        return False
