from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session, joinedload

from app.core import errors
from app.core.config import settings
from app.core.rate_limit import SlidingWindowRateLimiter, client_ip
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.platform_integration import PlatformIntegration
from app.models.webhook_event import WebhookEvent
from app.schemas.website import WebsiteChatRequest, WebsiteChatResponse
from app.services import instagram, media, messenger, telegram
from app.services.chat_service import generate_reply, persist_customer_message
from app.services.messaging import InboundMessage

logger = logging.getLogger(__name__)

router = APIRouter()

# Throttles the public website widget endpoint (paid AI calls). Per-process.
website_limiter = SlidingWindowRateLimiter(
    max_requests=settings.WEBSITE_RATE_LIMIT,
    window_seconds=settings.WEBSITE_RATE_WINDOW_SECONDS,
)

# Meta platforms whose webhooks only carry an opaque sender id, so the real
# display name must be fetched when a customer is first seen. Mapped to the
# service module (not the function) so the lookup happens at call time.
_PROFILE_SERVICES = {
    "messenger": messenger,
    "instagram": instagram,
}


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
    db: Session, user_id: UUID, customer_id: UUID, platform: str, integration_id: UUID
) -> Conversation:
    conversation = (
        db.query(Conversation)
        # Attachments come with the messages: the AI pipeline reads them to
        # re-send recent photos, and lazy-loading would issue one query per
        # message in the history window.
        .options(joinedload(Conversation.messages).joinedload(Message.attachments))
        .filter(
            Conversation.user_id == user_id,
            Conversation.customer_id == customer_id,
            Conversation.platform == platform,
            Conversation.status == "open",
        )
        .first()
    )
    if conversation:
        # Backfill for threads that predate the column, so a seller's reply can
        # be routed back through the bot the customer actually messaged.
        if conversation.integration_id is None:
            conversation.integration_id = integration_id
        return conversation

    conversation = Conversation(
        user_id=user_id,
        customer_id=customer_id,
        platform=platform,
        integration_id=integration_id,
    )
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


@dataclass
class InboundResult:
    """Outcome of handling one inbound message.

    ``reply`` is only set when the AI actually answered. ``paused`` separates
    "a human is handling this, stay quiet" from a genuine failure, which the
    website widget needs to tell apart so it doesn't show visitors an error.
    """

    reply: str | None = None
    paused: bool = False


async def _reply_to_inbound(
    db: Session, integration: PlatformIntegration, inbound: InboundMessage
) -> InboundResult:
    """Run the AI pipeline for one inbound message.

    Returns an empty result if the event is a duplicate or the AI/DB step fails
    (both logged), so the caller can ack the webhook without sending anything.
    """
    platform = integration.platform
    if not _claim_event(db, integration.id, inbound.event_id):
        logger.info("Skipping duplicate %s event %s", platform, inbound.event_id)
        return InboundResult()

    customer = _find_customer(db, integration.user_id, platform, inbound.external_user_id)
    if customer is None:
        name = inbound.sender_name
        # Meta platforms only carry an opaque id, so look up the real name once,
        # when the customer is first seen.
        profile_service = _PROFILE_SERVICES.get(platform)
        if profile_service is not None:
            resolved = await profile_service.get_profile_name(
                integration.access_token, inbound.external_user_id
            )
            name = resolved or name
        customer = _create_customer(
            db, integration.user_id, platform, inbound.external_user_id, name
        )

    conversation = _get_or_create_conversation(
        db, integration.user_id, customer.id, platform, integration.id
    )

    # Copy any photo onto our own storage before it touches the DB: the
    # platform's own link either carries the bot token or expires.
    image_url: str | None = None
    if inbound.has_image:
        try:
            image_url = await media.store_inbound_image(
                platform, integration.access_token, inbound.image_ref
            )
        except media.MediaError:
            # Fall through as a text-only message. Losing the photo is bad;
            # dropping the customer's message entirely would be worse.
            logger.exception(
                "Could not store inbound %s image for integration %s",
                platform,
                integration.id,
            )

    # A human has taken this thread over — record what the customer said, but
    # don't let the bot answer over the top of them.
    if not conversation.ai_enabled:
        try:
            persist_customer_message(db, conversation, inbound.text, image_url=image_url)
        except Exception:
            logger.exception(
                "Failed to record inbound message for paused conversation %s",
                conversation.id,
            )
        return InboundResult(paused=True)

    try:
        _customer_msg, ai_msg = await generate_reply(
            db, conversation, integration.user, inbound.text, image_url=image_url
        )
    except Exception:
        logger.exception("AI pipeline failed for %s integration %s", platform, integration.id)
        return InboundResult()
    return InboundResult(reply=ai_msg.content)


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

    result = await _reply_to_inbound(db, integration, inbound)
    if result.reply is None:
        # Ack anyway; retrying won't help an AI/DB failure and would double-post.
        # A paused conversation lands here too — the message is recorded, the
        # bot just stays quiet so the seller can answer it themselves.
        return {"ok": True, "handled": False, "paused": result.paused}

    try:
        await telegram.send_message(
            integration.access_token, inbound.external_user_id, result.reply
        )
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


async def _process_meta_webhook(
    db: Session,
    integration: PlatformIntegration,
    request: Request,
    signature: str | None,
    service,  # messenger or instagram module (same verify/parse/send interface)
) -> Response:
    """Shared handler for Meta platforms (Messenger, Instagram).

    Both use the same HMAC signature, webhook shape, and expect a plain
    "EVENT_RECEIVED" ack. Only the send transport (``service``) differs.
    """
    raw_body = await request.body()
    if not service.verify_signature(integration.app_secret, raw_body, signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    for inbound in service.parse_updates(await request.json()):
        result = await _reply_to_inbound(db, integration, inbound)
        if result.reply is None:
            continue  # duplicate, failure, or a thread a human has taken over
        try:
            await service.send_message(
                integration.access_token, inbound.external_user_id, result.reply
            )
        except Exception:
            logger.exception(
                "Failed to send %s reply for integration %s", integration.platform, integration.id
            )

    return Response(content="EVENT_RECEIVED", media_type="text/plain")


@router.post("/messenger/{integration_id}")
async def messenger_webhook(
    integration_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive Messenger events for one seller's page and auto-reply."""
    integration = _get_active_integration(db, integration_id, "messenger")
    return await _process_meta_webhook(db, integration, request, x_hub_signature_256, messenger)


@router.get("/instagram/{integration_id}")
async def instagram_verify(
    integration_id: UUID,
    db: Session = Depends(get_db),
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Instagram subscription handshake: echo hub.challenge when the token matches."""
    integration = _get_active_integration(db, integration_id, "instagram")
    if hub_mode == "subscribe" and hub_verify_token == integration.secret_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@router.post("/instagram/{integration_id}")
async def instagram_webhook(
    integration_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive Instagram DM events for one seller's account and auto-reply."""
    integration = _get_active_integration(db, integration_id, "instagram")
    return await _process_meta_webhook(db, integration, request, x_hub_signature_256, instagram)


def _enforce_website_origin(integration: PlatformIntegration, request: Request) -> None:
    """If the integration configures an origin allowlist, enforce it.

    For website integrations, ``secret_token`` may hold a comma-separated list
    of allowed origins (e.g. "https://shop.example.com"). When set, the request
    Origin must match — a server-side guard beyond browser CORS, since a public
    endpoint triggers paid AI calls.
    """
    allow = integration.secret_token
    if not allow:
        return  # no allowlist configured — open (dev / trusted use)
    allowed = {o.strip() for o in allow.split(",") if o.strip()}
    origin = request.headers.get("origin")
    if origin not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed")


@router.post("/website/{integration_id}", response_model=WebsiteChatResponse)
async def website_chat(
    integration_id: UUID,
    payload: WebsiteChatRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Synchronous chat endpoint for the on-site widget.

    Unlike the platform webhooks, the AI reply is returned directly in the HTTP
    response (the browser widget shows it), so there is no outbound send step
    and no redelivery to de-duplicate.
    """
    integration = _get_active_integration(db, integration_id, "website")
    _enforce_website_origin(integration, request)

    if not website_limiter.allow(f"{integration_id}:{client_ip(request)}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests — please slow down",
        )

    inbound = InboundMessage(
        external_user_id=payload.session_id,
        sender_name=payload.name or "Website visitor",
        text=payload.message,
        event_id=None,  # synchronous — nothing to dedupe
    )
    result = await _reply_to_inbound(db, integration, inbound)
    if result.paused:
        # The seller is handling this visitor themselves. Their message is
        # recorded; the widget has no channel to push a human reply back down,
        # so tell it to stop expecting one rather than showing an error.
        return WebsiteChatResponse(reply=None, paused=True)
    if result.reply is None:
        raise errors.assistant_unavailable()
    return WebsiteChatResponse(reply=result.reply)
