from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.platform_integration import PlatformIntegration
from app.models.webhook_event import WebhookEvent
from app.services import messenger, telegram
from app.services.chat_service import generate_reply
from app.services.messaging import InboundMessage

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_active_integration(db: Session, integration_id: UUID, platform: str) -> PlatformIntegration:
    integration = db.query(PlatformIntegration).filter(
        PlatformIntegration.id == integration_id,
        PlatformIntegration.platform == platform,
        PlatformIntegration.is_active == True,
    ).first()
    if not integration:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")
    return integration


def _find_customer(db: Session, user_id: UUID, platform: str, external_user_id: str) -> Customer | None:
    return db.query(Customer).filter(
        Customer.user_id == user_id,
        Customer.platform == platform,
        Customer.platform_id == external_user_id,
    ).first()


def _create_customer(
    db: Session, user_id: UUID, platform: str, external_user_id: str, name: str
) -> Customer:
    customer = Customer(
        user_id=user_id,
        name=name,
        platform=platform,
        platform_id=external_user_id,
    )
    db.add(customer)
    db.flush()
    return customer


def _get_or_create_conversation(
    db: Session, user_id: UUID, customer_id: UUID, platform: str
) -> Conversation:
    conversation = (
        db.query(Conversation)
        .options(joinedload(Conversation.messages))
        .filter(
            Conversation.user_id == user_id,
            Conversation.customer_id == customer_id,
            Conversation.platform == platform,
            Conversation.status == "open",
        )
        .first()
    )
    if conversation:
        return conversation

    conversation = Conversation(user_id=user_id, customer_id=customer_id, platform=platform)
    db.add(conversation)
    db.flush()
    return conversation


def _claim_event(db: Session, integration_id: UUID, event_id: str | None) -> bool:
    """Reserve an inbound event for processing; return False if already seen.

    The WebhookEvent row is flushed within the caller's transaction and gets
    committed alongside the reply — so a failed reply rolls the claim back and
    a genuine redelivery can retry, while a successful one blocks duplicates.
    """
    if event_id is None:
        return True  # nothing to dedupe on — process it
    seen = (
        db.query(WebhookEvent.id)
        .filter(WebhookEvent.integration_id == integration_id, WebhookEvent.event_id == event_id)
        .first()
    )
    if seen:
        return False
    db.add(WebhookEvent(integration_id=integration_id, event_id=event_id))
    db.flush()
    return True


async def _reply_to_inbound(
    db: Session, integration: PlatformIntegration, inbound: InboundMessage
) -> str | None:
    """Run the AI pipeline for one inbound message; return the reply text.

    Returns None if the event is a duplicate or the AI/DB step fails (both
    logged), so the caller can ack the webhook without sending anything.
    """
    platform = integration.platform
    if not _claim_event(db, integration.id, inbound.event_id):
        logger.info("Skipping duplicate %s event %s", platform, inbound.event_id)
        return None

    customer = _find_customer(db, integration.user_id, platform, inbound.external_user_id)
    if customer is None:
        name = inbound.sender_name
        # Messenger webhooks only carry the PSID, so look up the real name once,
        # when the customer is first seen.
        if platform == "messenger":
            resolved = await messenger.get_profile_name(
                integration.access_token, inbound.external_user_id
            )
            name = resolved or name
        customer = _create_customer(
            db, integration.user_id, platform, inbound.external_user_id, name
        )

    conversation = _get_or_create_conversation(db, integration.user_id, customer.id, platform)
    try:
        _customer_msg, ai_msg = await generate_reply(
            db, conversation, integration.user, inbound.text
        )
    except Exception:
        logger.exception("AI pipeline failed for %s integration %s", platform, integration.id)
        return None
    return ai_msg.content


@router.post("/telegram/{integration_id}")
async def telegram_webhook(
    integration_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Receive Telegram Bot API updates for one seller's bot and auto-reply.

    The unique {integration_id} in the URL identifies which seller owns the bot
    (Telegram lets each bot point at any webhook URL). The secret token header
    is verified so only Telegram can post here.
    """
    integration = _get_active_integration(db, integration_id, "telegram")

    if integration.secret_token and x_telegram_bot_api_secret_token != integration.secret_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret token")

    update = await request.json()
    inbound = telegram.parse_update(update)
    if inbound is None:
        # Non-text / unsupported update — ack so Telegram stops retrying
        return {"ok": True, "handled": False}

    reply = await _reply_to_inbound(db, integration, inbound)
    if reply is None:
        # Ack anyway; retrying won't help an AI/DB failure and would double-post
        return {"ok": True, "handled": False}

    try:
        await telegram.send_message(integration.access_token, inbound.external_user_id, reply)
    except Exception:
        logger.exception("Failed to send telegram reply for integration %s", integration_id)

    return {"ok": True, "handled": True}


@router.get("/messenger/{integration_id}")
async def messenger_verify(
    integration_id: UUID,
    db: Session = Depends(get_db),
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Messenger subscription handshake: echo hub.challenge when the token matches."""
    integration = _get_active_integration(db, integration_id, "messenger")
    if hub_mode == "subscribe" and hub_verify_token == integration.secret_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@router.post("/messenger/{integration_id}")
async def messenger_webhook(
    integration_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive Messenger events for one seller's page and auto-reply."""
    integration = _get_active_integration(db, integration_id, "messenger")

    raw_body = await request.body()
    if not messenger.verify_signature(integration.app_secret, raw_body, x_hub_signature_256):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    payload = await request.json()
    inbound_messages = messenger.parse_updates(payload)

    handled = 0
    for inbound in inbound_messages:
        reply = await _reply_to_inbound(db, integration, inbound)
        if reply is None:
            continue
        try:
            await messenger.send_message(integration.access_token, inbound.external_user_id, reply)
            handled += 1
        except Exception:
            logger.exception("Failed to send messenger reply for integration %s", integration_id)

    # Messenger expects a 200 with "EVENT_RECEIVED" to stop retries
    return Response(content="EVENT_RECEIVED", media_type="text/plain")
