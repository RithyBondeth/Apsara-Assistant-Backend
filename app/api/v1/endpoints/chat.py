from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.database import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product import Product
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.message import MessageOut
from app.services.ai_service import (
    build_openai_messages,
    build_system_prompt,
    generate_ai_reply,
    normalize_message,
)

router = APIRouter()


@router.post("/{conversation_id}", response_model=ChatResponse)
async def chat(
    conversation_id: UUID,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ── 1. Load conversation ─────────────────────────────────────────────────
    conversation = (
        db.query(Conversation)
        .options(joinedload(Conversation.messages))
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.status == "closed":
        raise HTTPException(status_code=400, detail="Conversation is closed")

    # ── 2. Normalize message & detect language ───────────────────────────────
    normalized_text, detected_lang = normalize_message(payload.message)

    # ── 3. Save customer message ─────────────────────────────────────────────
    customer_msg = Message(
        conversation_id=conversation_id,
        sender_type="customer",
        message_type=payload.message_type,
        content=normalized_text,
    )
    db.add(customer_msg)
    db.flush()  # get the ID before committing

    # ── 4. Load seller's active products ────────────────────────────────────
    products = (
        db.query(Product)
        .filter(Product.user_id == current_user.id, Product.is_active == True)
        .all()
    )

    # ── 5. Build system prompt ───────────────────────────────────────────────
    system_prompt = build_system_prompt(current_user, products, detected_lang)

    # ── 6. Build conversation history for OpenAI ────────────────────────────
    # Use messages already persisted (excludes the message we just flushed)
    history = [m for m in conversation.messages if m.id != customer_msg.id]
    openai_messages = build_openai_messages(system_prompt, history, normalized_text)

    # ── 7. Generate AI reply ─────────────────────────────────────────────────
    try:
        reply_text = await generate_ai_reply(openai_messages)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}")

    # ── 8. Save AI reply ─────────────────────────────────────────────────────
    ai_msg = Message(
        conversation_id=conversation_id,
        sender_type="assistant",
        message_type="text",
        content=reply_text,
    )
    db.add(ai_msg)

    # Bump conversation so it surfaces at the top of the list
    conversation.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(customer_msg)
    db.refresh(ai_msg)

    return ChatResponse(
        customer_message=MessageOut.model_validate(customer_msg),
        ai_message=MessageOut.model_validate(ai_msg),
    )
