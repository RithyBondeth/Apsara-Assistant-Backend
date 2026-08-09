from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product import Product
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.message import MessageOut
from app.services.ai_service import (
    CATALOGUE_LIMIT,
    HISTORY_LIMIT,
    AIError,
    build_openai_messages,
    build_system_prompt,
    generate_ai_reply,
)

router = APIRouter()


@router.post("/{conversation_id}", response_model=ChatResponse)
def chat(
    conversation_id: UUID,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record an incoming customer message and reply to it with the assistant."""
    # ── 1. Load conversation ─────────────────────────────────────────────────
    conversation = (
        db.query(Conversation)
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

    content = payload.message.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # ── 2. Save the customer message ─────────────────────────────────────────
    # Committed before the model is called: a customer wrote it, so it belongs
    # in the thread whether or not the reply succeeds. Rolling it back on an AI
    # outage would silently discard what they said.
    customer_msg = Message(
        conversation_id=conversation_id,
        sender_type="customer",
        message_type=payload.message_type,
        content=content,
    )
    db.add(customer_msg)
    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(customer_msg)

    # ── 3. Load the seller's active catalogue ────────────────────────────────
    # Bounded in the query, not just when the prompt is built — a seller with a
    # large catalogue should not pull all of it into memory on every message.
    products = (
        db.query(Product)
        .filter(Product.user_id == current_user.id, Product.is_active == True)
        .order_by(Product.created_at.desc())
        .limit(CATALOGUE_LIMIT)
        .all()
    )

    # ── 4. Load recent history, newest-last ──────────────────────────────────
    # Trimmed in SQL rather than loading every message in the conversation and
    # slicing in Python, which grows without bound as a thread gets longer.
    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    history = list(reversed(recent))

    # ── 5. Generate the reply ────────────────────────────────────────────────
    system_prompt = build_system_prompt(current_user, products)
    try:
        reply_text = generate_ai_reply(build_openai_messages(system_prompt, history))
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ── 6. Save the AI reply ─────────────────────────────────────────────────
    ai_msg = Message(
        conversation_id=conversation_id,
        sender_type="assistant",
        message_type="text",
        content=reply_text,
    )
    db.add(ai_msg)
    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ai_msg)

    return ChatResponse(
        customer_message=MessageOut.model_validate(customer_msg),
        ai_message=MessageOut.model_validate(ai_msg),
    )
