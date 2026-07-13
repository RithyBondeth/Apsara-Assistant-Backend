from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.database import get_db
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.message import MessageOut
from app.services.chat_service import generate_reply

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

    # ── 2. Run the shared AI pipeline (persists both messages) ───────────────
    try:
        customer_msg, ai_msg = await generate_reply(
            db, conversation, current_user, payload.message, payload.message_type
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}")

    return ChatResponse(
        customer_message=MessageOut.model_validate(customer_msg),
        ai_message=MessageOut.model_validate(ai_msg),
    )
