"""Turning a webhook message into a conversation turn, and answering it.

Runs after the webhook has been acknowledged, so it owns its own database
session and swallows its own failures — there is no request left to fail.
"""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.clock import utcnow
from app.core.config import settings
from app.database import SessionLocal
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.attachment import Attachment
from app.models.message import Message
from app.models.platform_connection import PlatformConnection
from app.models.order import PAID, Order
from app.models.product import Product
from app.models.user import User
from app.services.ai_service import (
    CATALOGUE_LIMIT,
    HISTORY_LIMIT,
    AIError,
    build_openai_messages,
    build_system_prompt,
    generate_ai_reply,
    payment_qr_message,
    split_needs_seller,
    split_payment_qr,
)
from app.services.platforms import (
    MESSENGER,
    SAFE_RECEIPT_TYPES,
    TEXT,
    VOICE,
    InboundMessage,
    InboundAttachment,
    download_attachment,
    fetch_messenger_profile,
    send_image,
    send_reply,
)
from app.services.queue import register
from app.services.quota import spend_reply
from app.services.payment_qrs import default_payment_qr_url
from app.services.receipts import queue_scan
from app.services.seller_alerts import flag_attention

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

    name = message.sender_name
    if not name and connection.platform == MESSENGER:
        # Messenger puts no name in the payload, so it takes a second call.
        # Only on first contact — afterwards the row already has one.
        name = fetch_messenger_profile(connection.access_token, message.sender_id)

    customer = Customer(
        user_id=connection.user_id,
        platform=connection.platform,
        platform_id=message.sender_id,
        # Still possible to come back empty: the lookup needs a permission the
        # customer may not have granted.
        name=name or f"Customer {message.sender_id[-6:]}",
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
            Conversation.platform_connection_id == connection.id,
            Conversation.status.in_(("open", "pending")),
        )
        .first()
    )
    if conversation:
        return conversation

    conversation = Conversation(
        user_id=connection.user_id,
        customer_id=customer.id,
        platform=connection.platform,
        platform_connection_id=connection.id,
        source="channel",
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
        .filter(
            Message.platform_connection_id == connection.id,
            Message.external_id == message.external_id,
        )
        .first()
        is not None
    )


INBOUND_MESSAGE = "inbound_message"


@register(INBOUND_MESSAGE)
def handle_inbound_job(payload: dict) -> None:
    """Queue entrypoint. Payload is plain JSON, so the message is rebuilt here."""
    message_payload = dict(payload["message"])
    message_payload["attachments"] = tuple(
        InboundAttachment(**attachment)
        for attachment in message_payload.get("attachments", [])
    )
    handle_inbound(payload["connection_id"], InboundMessage(**message_payload))


def handle_inbound(connection_id, message: InboundMessage) -> None:
    """Record an inbound message and, if enabled, answer it.

    Takes a connection id rather than an instance: the row is re-read here,
    since this runs long after the request that received it.

    Safe to run twice. The queue releases jobs abandoned by a dead worker, and
    the platforms redeliver on their own — the external message id is what
    makes a repeat a no-op rather than a second reply.
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

        stored_attachments: list[Attachment] = []
        stored_bytes = 0
        for locator in message.attachments:
            try:
                downloaded = download_attachment(
                    connection.platform, connection.access_token, locator
                )
                if stored_bytes + len(downloaded.content) > settings.MAX_ATTACHMENT_BYTES:
                    raise ValueError("Attachments exceed the per-message size limit")
                stored_attachments.append(Attachment(
                    blob=downloaded.content,
                    file_type=downloaded.content_type,
                    file_name=downloaded.file_name,
                    file_size=len(downloaded.content),
                    review_status="pending",
                ))
                stored_bytes += len(downloaded.content)
            except Exception as exc:
                logger.warning("Could not retain attachment from %s message %s: %s",
                               connection.platform, message.external_id, exc)

        image_stored = any(a.file_type in SAFE_RECEIPT_TYPES for a in stored_attachments)
        # The type says what arrived even when nothing could be kept: a voice
        # note whose download failed is still a voice note the seller must
        # know about, not a blank line.
        message_type = ("image" if image_stored
                        else message.kind if message.kind != TEXT else "text")
        inbound = Message(
            conversation_id=conversation.id,
            platform_connection_id=connection.id,
            sender_type="customer",
            message_type=message_type,
            content=message.text or ("Attachment could not be downloaded."
                                     if message.kind == TEXT and message.attachments
                                     and not stored_attachments else None),
            external_id=message.external_id,
        )
        inbound.attachments.extend(stored_attachments)
        db.add(inbound)
        now = utcnow()
        conversation.updated_at = now
        conversation.last_customer_message_at = now
        conversation.unread_count = (conversation.unread_count or 0) + 1
        if conversation.first_customer_message_at is None:
            conversation.first_customer_message_at = now
        # Committed before the model is called: the customer's message belongs
        # in the thread whether or not a reply can be produced.
        try:
            db.commit()
        except IntegrityError:
            # A redelivery can be claimed concurrently by two workers. The
            # database uniqueness constraint is the final idempotency guard.
            db.rollback()
            logger.info("Ignoring concurrently handled %s message %s",
                        connection.platform, message.external_id)
            return

        # A photo while an order is awaiting payment is, almost always, the
        # receipt. The seller is told straight away; nothing below depends on
        # whether the assistant also has a caption to answer.
        receipt_for = _order_awaiting_payment(db, conversation) if stored_attachments else None
        if receipt_for is not None:
            # Read the receipt first, then alert the seller with what it says.
            queue_scan(db, receipt_for, [a.id for a in stored_attachments], customer.name)
            db.commit()

        # The assistant cannot hear or watch. Whatever else happens with this
        # message, the seller is the only one who can answer it.
        if message.kind == VOICE:
            _needs_seller(db, conversation, customer, "attention_voice", message.text)
            if not message.text:
                return
        elif message.kind != TEXT and not message.text:
            _needs_seller(db, conversation, customer, "attention_unsupported", None)
            return

        # A receipt without a caption is evidence, not a prompt for the model.
        if conversation.handling_mode == "manual" or not connection.auto_reply:
            # The receipt alert already said who and why; only a message with
            # something more to say earns a second one.
            if receipt_for is None or message.text:
                _needs_seller(db, conversation, customer, "attention_manual", message.text)
            return
        if not message.text:
            if stored_attachments and receipt_for is None:
                _needs_seller(db, conversation, customer, "attention_photo", None)
            return

        # Charged before generating, not after: the cost is incurred by asking,
        # so a reply that fails to generate still counts against the day.
        if not spend_reply(db, connection.user_id):
            logger.warning("Daily reply limit reached; %s message stored unanswered",
                           connection.platform)
            _needs_seller(db, conversation, customer, "attention_unanswered", message.text)
            return

        generated = _generate(db, connection, conversation)
        if generated is None:
            _needs_seller(db, conversation, customer, "attention_unanswered", message.text)
            return
        reply, qr_url, wants_seller = generated

        # Nothing below stores a message the platform refused: the customer
        # never saw it, so recording it would leave the seller reading a
        # thread that did not happen.
        delivered = False

        if reply:
            if not send_reply(connection.platform, connection.access_token,
                              message.sender_id, reply):
                logger.error("Reply generated but not delivered for conversation %s",
                             conversation.id)
                # A payment QR arriving with no message explaining it is
                # worse than nothing, so it does not follow a failed reply.
                _needs_seller(db, conversation, customer, "attention_unanswered", message.text)
                return
            db.add(Message(
                conversation_id=conversation.id,
                sender_type="assistant",
                message_type="text",
                content=reply,
            ))
            delivered = True

        if qr_url:
            if send_image(connection.platform, connection.access_token,
                          message.sender_id, qr_url):
                db.add(payment_qr_message(conversation.id, qr_url))
                delivered = True
            else:
                # The text has already gone out, so the customer is not left
                # in silence — they are left waiting on an image the seller
                # needs to know never arrived.
                logger.error("Payment QR not delivered for conversation %s",
                             conversation.id)

        if delivered:
            now = utcnow()
            conversation.updated_at = now
            if conversation.first_response_at is None:
                conversation.first_response_at = now
            if wants_seller:
                # The customer has been told the seller will get back to
                # them; this is what makes that true.
                flag_attention(db, conversation, "attention_escalated",
                               customer.name, message.text)
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to handle an inbound %s message", connection_id)
    finally:
        db.close()


def _needs_seller(db: Session, conversation: Conversation, customer: Customer,
                  kind: str, text: str | None) -> None:
    """Flag the thread for the seller and commit — the customer's message is
    already stored, and this must survive whatever happens next."""
    flag_attention(db, conversation, kind, customer.name, text)
    db.commit()


def _order_awaiting_payment(db: Session, conversation: Conversation) -> Order | None:
    """The newest live order on this thread that has not been paid, if any."""
    return (
        db.query(Order)
        .filter(Order.conversation_id == conversation.id,
                Order.payment_status != PAID,
                Order.status != "cancelled")
        .order_by(Order.created_at.desc())
        .first()
    )


def _generate(db: Session, connection: PlatformConnection,
              conversation: Conversation) -> tuple[str, str | None, bool] | None:
    """The reply text, the QR to attach if the assistant asked for one, and
    whether it asked for the seller.

    `None` means no reply could be produced at all — distinct from a reply
    that carries neither.
    """
    seller = db.query(User).filter(User.id == connection.user_id).first()
    if not seller:
        return None

    products = (
        db.query(Product).options(selectinload(Product.variants))
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
        raw = generate_ai_reply(
            build_openai_messages(build_system_prompt(seller, products),
                                  list(reversed(recent)))
        )
    except AIError:
        # Already logged with its cause. The customer's message is stored, so
        # the seller sees it in the inbox and can answer by hand.
        logger.warning("No reply generated for conversation %s", conversation.id)
        return None

    reply, wants_qr = split_payment_qr(raw)
    reply, wants_seller = split_needs_seller(reply)
    # Guarded against the seller having cleared their QR since the prompt was
    # built, and against a model that emits the marker regardless.
    return reply, default_payment_qr_url(seller) if wants_qr else None, wants_seller
