from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product import Product
from app.models.user import User
from app.services.ai_service import (
    build_openai_messages,
    build_system_prompt,
    generate_ai_reply,
    normalize_message,
)


async def generate_reply(
    db: Session,
    conversation: Conversation,
    user: User,
    text: str,
    message_type: str = "text",
) -> tuple[Message, Message]:
    """Run the Khmer-aware AI pipeline for one inbound customer message.

    Persists the customer message and the assistant reply, bumps the
    conversation timestamp, and returns ``(customer_message, ai_message)``.

    Shared by the authenticated chat endpoint and the platform webhooks so the
    pipeline stays identical regardless of channel. Raises on AI failure after
    rolling back, leaving no partial customer message behind.
    """
    normalized_text, detected_lang = normalize_message(text)

    customer_msg = Message(
        conversation_id=conversation.id,
        sender_type="customer",
        message_type=message_type,
        content=normalized_text,
    )
    db.add(customer_msg)
    db.flush()  # assign an id before building history

    products = (
        db.query(Product)
        .filter(Product.user_id == user.id, Product.is_active == True)
        .all()
    )

    system_prompt = build_system_prompt(user, products, detected_lang)
    history = [m for m in conversation.messages if m.id != customer_msg.id]
    openai_messages = build_openai_messages(system_prompt, history, normalized_text)

    try:
        reply_text = await generate_ai_reply(openai_messages)
    except Exception:
        db.rollback()
        raise

    ai_msg = Message(
        conversation_id=conversation.id,
        sender_type="assistant",
        message_type="text",
        content=reply_text,
    )
    db.add(ai_msg)
    conversation.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(customer_msg)
    db.refresh(ai_msg)
    return customer_msg, ai_msg
