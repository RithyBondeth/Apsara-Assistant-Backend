"""Turning a webhook message into a conversation turn, and answering it.

Runs after the webhook has been acknowledged, so it owns its own database
session and swallows its own failures — there is no request left to fail.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.platform_connection import PlatformConnection
from app.models.product import Product
from app.models.user import User
from app.services.ai_service import (
    CATALOGUE_LIMIT,
    HISTORY_LIMIT,
    AIError,
    build_openai_messages,
    build_system_prompt,
    generate_ai_reply,
)
from app.services.platforms import InboundMessage, send_reply

logger = logging.getLogger(__name__)


def _customer_for(db: Session, connection: PlatformConnection,
                  message: InboundMessage) -> Customer:
    """Find the sender, or record them as a new customer.

    Matched on (seller, platform, platform_id) — the same triple the customers
    endpoint dedupes on, so a customer created here and one added by hand are
    the same row.
    """
    customer = (
        db.query(Customer)
        .filter(
            Customer.user_id == connection.user_id,
            Customer.platform == connection.platform,
            Customer.platform_id == message.sender_id,
        )
        .first()
    )
    if customer:
        # Telegram supplies a display name; Messenger does not without a
        # separate profile call. Fill it in once it is known, but never
        # overwrite a name the seller has since corrected by hand.
        if message.sender_name and customer.name.startswith("Customer "):
            customer.name = message.sender_name
        return customer

    customer = Customer(
        user_id=connection.user_id,
        platform=connection.platform,
        platform_id=message.sender_id,
        # Messenger gives no name up front, so fall back to something the
        # seller can recognise until they rename it.
        name=message.sender_name or f"Customer {message.sender_id[-6:]}",
    )
    db.add(customer)
    db.flush()
    return customer


def _conversation_for(db: Session, connection: PlatformConnection,
                      customer: Customer) -> Conversation:
    """Reuse the customer's open thread, or start one.

    Deliberately matches the conversations endpoint: a closed conversation
    stays closed as a record, and a customer writing again opens a new thread
    rather than reviving one the seller had finished with.
    """
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.user_id == connection.user_id,
            Conversation.customer_id == customer.id,
            Conversation.platform == connection.platform,
            Conversation.status == "open",
        )
        .first()
    )
    if conversation:
        return conversation

    conversation = Conversation(
        user_id=connection.user_id,
        customer_id=customer.id,
        platform=connection.platform,
    )
    db.add(conversation)
    db.flush()
    return conversation


def _already_handled(db: Session, connection: PlatformConnection,
                     message: InboundMessage) -> bool:
    """True if this platform message is a redelivery we have already stored.

    Both platforms retry whenever they do not get a prompt 2xx, so this is the
    difference between a duplicate and a second AI reply the seller pays for.
    """
    return (
        db.query(Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.user_id == connection.user_id,
            Conversation.platform == connection.platform,
            Message.external_id == message.external_id,
        )
        .first()
        is not None
    )


def handle_inbound(connection_id, message: InboundMessage) -> None:
    """Record an inbound message and, if enabled, answer it.

    Takes a connection id rather than an instance: the caller's session is
    already closed by the time this runs in the background.
    """
    db = SessionLocal()
    try:
        connection = (
            db.query(PlatformConnection)
            .filter(PlatformConnection.id == connection_id,
                    PlatformConnection.is_active == True)
            .first()
        )
        if not connection:
            logger.warning("Inbound message for unknown connection %s", connection_id)
            return

        if _already_handled(db, connection, message):
            logger.info("Ignoring redelivered %s message %s",
                        connection.platform, message.external_id)
            return

        customer = _customer_for(db, connection, message)
        conversation = _conversation_for(db, connection, customer)

        db.add(Message(
            conversation_id=conversation.id,
            sender_type="customer",
            message_type="text",
            content=message.text,
            external_id=message.external_id,
        ))
        conversation.updated_at = datetime.utcnow()
        # Committed before the model is called: the customer's message belongs
        # in the thread whether or not a reply can be produced.
        db.commit()

        if not connection.auto_reply:
            return

        reply = _generate(db, connection, conversation)
        if reply is None:
            return

        if send_reply(connection.platform, connection.access_token,
                      message.sender_id, reply):
            db.add(Message(
                conversation_id=conversation.id,
                sender_type="assistant",
                message_type="text",
                content=reply,
            ))
            conversation.updated_at = datetime.utcnow()
            db.commit()
        else:
            # Not stored: the customer never saw it, so recording it would
            # leave the seller reading a thread that did not happen.
            logger.error("Reply generated but not delivered for conversation %s",
                         conversation.id)
    except Exception:
        db.rollback()
        logger.exception("Failed to handle an inbound %s message", connection_id)
    finally:
        db.close()


def _generate(db: Session, connection: PlatformConnection,
              conversation: Conversation) -> str | None:
    seller = db.query(User).filter(User.id == connection.user_id).first()
    if not seller:
        return None

    products = (
        db.query(Product)
        .filter(Product.user_id == seller.id, Product.is_active == True)
        .order_by(Product.created_at.desc())
        .limit(CATALOGUE_LIMIT)
        .all()
    )
    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )

    try:
        return generate_ai_reply(
            build_openai_messages(build_system_prompt(seller, products),
                                  list(reversed(recent)))
        )
    except AIError:
        # Already logged with its cause. The customer's message is stored, so
        # the seller sees it in the inbox and can answer by hand.
        logger.warning("No reply generated for conversation %s", conversation.id)
        return None
