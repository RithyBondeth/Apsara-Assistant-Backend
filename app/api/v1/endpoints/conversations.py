from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core.platforms import SUPPORTED_PLATFORMS, platform_list
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.message import MessageCreate, MessageOut
from app.services import outbound

router = APIRouter()


def _needs_me_clause():
    """SQL form of "the seller has to deal with this": escalated, or unread.

    This duplicates ``Conversation.unread``, which is a Python property and so
    can't be used in a WHERE clause. The two must agree —
    ``test_attention.py::test_sql_filter_matches_the_unread_property`` fails if
    they ever drift apart.
    """
    unread = and_(
        Conversation.last_customer_message_at.isnot(None),
        or_(
            Conversation.last_seen_at.is_(None),
            Conversation.last_customer_message_at > Conversation.last_seen_at,
        ),
    )
    return or_(Conversation.needs_attention.is_(True), unread)


# ── Conversations ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ConversationOut])
def list_conversations(
    skip: int = 0,
    limit: int = 50,
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    needs_me: bool = Query(
        default=False,
        description="Only threads the seller has to deal with: the AI escalated "
        "or failed, or the customer has said something since they last looked.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Conversation).filter(Conversation.user_id == current_user.id)
    if status:
        query = query.filter(Conversation.status == status)
    if platform:
        query = query.filter(Conversation.platform == platform)
    if customer_id:
        query = query.filter(Conversation.customer_id == customer_id)
    if needs_me:
        query = query.filter(_needs_me_clause())
    return query.order_by(Conversation.updated_at.desc()).offset(skip).limit(limit).all()


@router.post("/{conversation_id}/seen", response_model=ConversationOut)
def mark_seen(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear the unread mark — the seller has this thread open.

    Its own endpoint rather than a side effect of GET: fetching a conversation
    shouldn't mutate it, or a prefetch would silently mark it read.

    Clears needs_attention too: the escalation has been seen, and leaving it
    set would make the "needs you" list permanent and therefore ignored.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conversation.last_seen_at = datetime.utcnow()
    conversation.needs_attention = False
    db.commit()
    db.refresh(conversation)
    return conversation


@router.post("/", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform. Supported: {platform_list()}",
        )

    customer = db.query(Customer).filter(
        Customer.id == payload.customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    existing = db.query(Conversation).filter(
        Conversation.user_id == current_user.id,
        Conversation.customer_id == payload.customer_id,
        Conversation.platform == payload.platform,
        Conversation.status == "open",
    ).first()
    if existing:
        return existing

    conversation = Conversation(
        user_id=current_user.id,
        customer_id=payload.customer_id,
        platform=payload.platform,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = (
        db.query(Conversation)
        .options(joinedload(Conversation.messages).joinedload(Message.attachments))
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.patch("/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(conversation, field, value)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    db.delete(conversation)  # messages cascade via the relationship
    db.commit()


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: UUID,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return (
        db.query(Message)
        .options(joinedload(Message.attachments))
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send the seller's own reply to the customer (human takeover).

    Delivery happens before the message is persisted, so a reply that never
    reached the customer never appears in the seller's inbox either.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.status == "closed":
        raise HTTPException(status_code=400, detail="Cannot send messages to a closed conversation")

    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    customer = db.query(Customer).filter(Customer.id == conversation.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        await outbound.send_to_customer(db, conversation, customer, content)
    except outbound.OutboundError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    message = Message(
        conversation_id=conversation_id,
        sender_type=payload.sender_type,
        message_type=payload.message_type,
        content=content,
    )
    db.add(message)
    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(message)
    return message
