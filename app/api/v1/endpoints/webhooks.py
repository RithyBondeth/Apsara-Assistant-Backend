from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.platform_integration import PlatformIntegration
from app.services import telegram
from app.services.chat_service import generate_reply
from app.services.telegram import InboundMessage

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


def _get_or_create_customer(
    db: Session, user_id: UUID, platform: str, inbound: InboundMessage
) -> Customer:
    customer = db.query(Customer).filter(
        Customer.user_id == user_id,
        Customer.platform == platform,
        Customer.platform_id == inbound.external_user_id,
    ).first()
    if customer:
        return customer

    customer = Customer(
        user_id=user_id,
        name=inbound.sender_name,
        platform=platform,
        platform_id=inbound.external_user_id,
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

    customer = _get_or_create_customer(db, integration.user_id, "telegram", inbound)
    conversation = _get_or_create_conversation(db, integration.user_id, customer.id, "telegram")

    try:
        _customer_msg, ai_msg = await generate_reply(
            db, conversation, integration.user, inbound.text
        )
    except Exception:
        logger.exception("AI pipeline failed for telegram integration %s", integration_id)
        # Ack anyway; retrying won't help an AI/DB failure and would double-post
        return {"ok": True, "handled": False}

    try:
        await telegram.send_message(integration.access_token, inbound.external_user_id, ai_msg.content)
    except Exception:
        logger.exception("Failed to send telegram reply for integration %s", integration_id)

    return {"ok": True, "handled": True}
