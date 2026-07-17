"""Deliver a seller's own reply back to the customer on their platform.

The webhooks already push the AI's replies out; this is the human-takeover
path, used when a seller answers from the dashboard inbox.

Telegram, Messenger and Instagram all expose ``send_message(access_token,
recipient_id, text)``, so dispatch is a straight table lookup. The website
widget is request/response — it has no channel to push to — so it is
deliberately absent from ``_SENDERS`` and callers must handle that.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.platform_integration import PlatformIntegration
from app.services import instagram, messenger, telegram

logger = logging.getLogger(__name__)

# platform -> service module (not the function, so tests can monkeypatch the
# module attribute and have it resolve at call time).
_SENDERS = {
    "telegram": telegram,
    "messenger": messenger,
    "instagram": instagram,
}

#: Platforms a seller reply can actually be pushed to.
PUSH_PLATFORMS = frozenset(_SENDERS)


class OutboundError(Exception):
    """A seller's reply could not be delivered to the customer."""


def _platform_reason(exc: httpx.HTTPStatusError) -> str:
    """Pull the platform's own explanation out of a failed send.

    Worth the effort because the common failure has a specific, actionable
    cause the seller can act on — Meta rejects replies sent more than 24 hours
    after the customer's last message (or 7 days without the Human Agent
    permission). "The messenger channel rejected the message" tells them
    nothing; Meta's own wording tells them to wait for the customer to write.
    """
    try:
        body = exc.response.json()
    except Exception:
        text = (exc.response.text or "").strip()
        return text[:200] or f"HTTP {exc.response.status_code}"

    if isinstance(body, dict):
        # Meta: {"error": {"message": "...", "code": 10, "error_subcode": ...}}
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        # Telegram: {"ok": false, "description": "..."}
        if body.get("description"):
            return str(body["description"])

    return f"HTTP {exc.response.status_code}"


def resolve_integration(
    db: Session, conversation: Conversation
) -> PlatformIntegration | None:
    """Find the bot/page to send this conversation's reply through.

    Prefers the integration the conversation arrived on. Falls back to the
    seller's first active integration for the platform, which covers rows
    created before ``integration_id`` existed. The fallback is only correct
    when the seller has one integration per platform; with several, the reply
    could otherwise go out from the wrong bot, so we record integration_id on
    new conversations to avoid relying on it.
    """
    if conversation.integration_id:
        integration = db.query(PlatformIntegration).filter(
            PlatformIntegration.id == conversation.integration_id,
            PlatformIntegration.is_active == True,
        ).first()
        if integration:
            return integration

    return db.query(PlatformIntegration).filter(
        PlatformIntegration.user_id == conversation.user_id,
        PlatformIntegration.platform == conversation.platform,
        PlatformIntegration.is_active == True,
    ).order_by(PlatformIntegration.created_at).first()


async def send_to_customer(
    db: Session, conversation: Conversation, customer: Customer, text: str
) -> None:
    """Push ``text`` to the customer as the seller. Raises OutboundError.

    Raising rather than swallowing is deliberate: the caller persists the
    message only if this succeeds, so the seller never sees a reply in their
    inbox that the customer never received.
    """
    platform = conversation.platform
    service = _SENDERS.get(platform)
    if service is None:
        raise OutboundError(
            f"Replies can't be delivered to the {platform} channel from here."
        )

    if not customer.platform_id:
        raise OutboundError("This customer has no platform ID to deliver to.")

    integration = resolve_integration(db, conversation)
    if integration is None:
        raise OutboundError(
            f"No active {platform} channel is connected to send this reply."
        )

    try:
        # human_agent marks this as a person answering, not the bot: Meta needs
        # the distinction to allow a reply outside the 24-hour window.
        await service.send_message(
            integration.access_token, customer.platform_id, text, human_agent=True
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Platform rejected seller reply for conversation %s via %s: %s",
            conversation.id,
            platform,
            exc.response.text[:500],
        )
        raise OutboundError(_platform_reason(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Failed to deliver seller reply for conversation %s via %s",
            conversation.id,
            platform,
        )
        raise OutboundError(f"Couldn't reach {platform} to send this reply.") from exc
