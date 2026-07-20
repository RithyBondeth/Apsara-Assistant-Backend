from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product import Product
from app.models.user import User
from app.services.ai_service import (
    build_openai_messages,
    build_system_prompt,
    detect_reply_language,
    generate_ai_reply,
    normalize_message,
    split_needs_seller,
)


def _message_type(default: str, image_url: str | None, audio_url: str | None) -> str:
    """Classify a message by the richest media it carries.

    Image wins over audio when both are present: a photo of the product is the
    thing a seller scanning a thread needs to see, and the audio is reachable
    from the attachment either way.
    """
    if image_url:
        return "image"
    if audio_url:
        return "audio"
    return default


def _attach_media(
    db: Session, message_id, image_url: str | None, audio_url: str | None
) -> None:
    """Record hosted media against a message. Caller must have flushed the id."""
    if image_url:
        db.add(Attachment(message_id=message_id, file_url=image_url, file_type="image"))
    if audio_url:
        db.add(Attachment(message_id=message_id, file_url=audio_url, file_type="audio"))


def persist_customer_message(
    db: Session,
    conversation: Conversation,
    text: str,
    message_type: str = "text",
    image_url: str | None = None,
    audio_url: str | None = None,
) -> Message:
    """Record an inbound customer message without answering it.

    Used when a conversation's AI is paused, or when a voice note transcribed
    too poorly to answer from: the seller still needs to see what the customer
    said — including any photo or audio, which is often the whole point of the
    message — the bot just shouldn't talk over them. Commits, which also
    commits the caller's webhook-event claim.
    """
    normalized_text, _lang = normalize_message(text)

    message = Message(
        conversation_id=conversation.id,
        sender_type="customer",
        message_type=_message_type(message_type, image_url, audio_url),
        content=normalized_text,
    )
    db.add(message)
    db.flush()  # assign an id for the attachment's FK

    _attach_media(db, message.id, image_url, audio_url)

    now = datetime.utcnow()
    conversation.last_customer_message_at = now
    conversation.updated_at = now
    db.commit()
    db.refresh(message)
    return message


async def generate_reply(
    db: Session,
    conversation: Conversation,
    user: User,
    text: str,
    message_type: str = "text",
    image_url: str | None = None,
    audio_url: str | None = None,
) -> tuple[Message, Message]:
    """Run the Khmer-aware AI pipeline for one inbound customer message.

    Persists the customer message and the assistant reply, bumps the
    conversation timestamp, and returns ``(customer_message, ai_message)``.

    ``image_url`` is an already-hosted image the customer sent (see
    services/media.py — never a raw platform URL). It is recorded as an
    Attachment and shown to the vision model.

    ``audio_url`` is a hosted voice note whose transcript is already folded
    into ``text`` by the caller. It is recorded as an Attachment so the seller
    can hear the original, but is not sent to the model — the transcript is
    what the model reasons over.

    Shared by the authenticated chat endpoint and the platform webhooks so the
    pipeline stays identical regardless of channel. On AI failure it still
    commits the customer's message with ``needs_attention`` set, then re-raises
    — the seller answers it by hand instead of the message disappearing.
    """
    normalized_text = text.strip()
    history = list(conversation.messages)
    # Not normalize_message(): a photo with no caption carries no language
    # signal, and defaulting such a customer to English would be a regression.
    detected_lang = detect_reply_language(normalized_text, history)

    customer_msg = Message(
        conversation_id=conversation.id,
        sender_type="customer",
        message_type=_message_type(message_type, image_url, audio_url),
        content=normalized_text,
    )
    db.add(customer_msg)
    db.flush()  # assign an id before building history

    _attach_media(db, customer_msg.id, image_url, audio_url)

    products = (
        db.query(Product)
        .filter(Product.user_id == user.id, Product.is_active == True)
        .all()
    )

    system_prompt = build_system_prompt(user, products, detected_lang)
    openai_messages = build_openai_messages(
        system_prompt,
        history,
        normalized_text,
        new_image_urls=[image_url] if image_url else None,
    )

    try:
        reply_text = await generate_ai_reply(openai_messages)
    except Exception:
        # Keep what the customer said and raise their hand, rather than
        # rolling it back. The webhook acks 200 either way, so the platform
        # never redelivers — a rollback here silently destroyed the message
        # and neither the customer nor the seller ever learned it was lost.
        conversation.needs_attention = True
        conversation.last_customer_message_at = datetime.utcnow()
        conversation.updated_at = datetime.utcnow()
        db.commit()
        raise

    # The marker is an internal signal — strip it before it reaches anyone.
    reply_text, needs_seller = split_needs_seller(reply_text)
    if needs_seller:
        conversation.needs_attention = True

    ai_msg = Message(
        conversation_id=conversation.id,
        sender_type="assistant",
        message_type="text",
        content=reply_text,
    )
    db.add(ai_msg)
    now = datetime.utcnow()
    conversation.last_customer_message_at = now
    conversation.updated_at = now

    db.commit()
    db.refresh(customer_msg)
    db.refresh(ai_msg)
    return customer_msg, ai_msg
