"""Helpers for driving the webhook endpoints."""

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest import mock

from app.core.config import settings

MESSENGER_URL = "/api/v1/webhooks/messenger"


def sign(body: bytes) -> str:
    digest = hmac.new(settings.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_messenger(client, payload: dict, signature: str | None = "auto"):
    """POST a Messenger payload, signing the exact bytes that are sent."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(MESSENGER_URL, content=body, headers=headers)


def messenger_payload(page_id: str, sender_id: str, text: str, mid: str = "mid.1",
                      **message_extra):
    return {
        "object": "page",
        "entry": [{
            "id": page_id,
            "messaging": [{
                "sender": {"id": sender_id},
                "recipient": {"id": page_id},
                "message": {"mid": mid, "text": text, **message_extra},
            }],
        }],
    }


def telegram_update(chat_id: int, text: str, update_id: int = 1, **from_extra):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "from": {"id": chat_id, "is_bot": False, "first_name": "Srey",
                     **from_extra},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def post_telegram(client, connection_id, payload: dict, secret: str | None):
    headers = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return client.post(f"/api/v1/webhooks/telegram/{connection_id}",
                       json=payload, headers=headers)


@contextmanager
def sends(ok: bool = True):
    """Stub outbound delivery, recording what would have been sent."""
    sent = []

    def _send(platform, token, recipient_id, text):
        sent.append({"platform": platform, "recipient_id": recipient_id, "text": text,
                     "token": token})
        return ok

    # Patched where it is used, not where it is defined.
    import app.services.inbound as inbound
    with mock.patch.object(inbound, "send_reply", _send):
        yield sent


def connect(client, seller, platform="messenger", external_id="page-1",
            token="page-token-abc", **extra):
    r = client.post("/api/v1/integrations/",
                    json={"platform": platform, "external_id": external_id,
                          "access_token": token, **extra},
                    headers=seller.headers)
    assert r.status_code == 201, r.text
    return r.json()
