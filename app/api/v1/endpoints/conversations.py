from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core.clock import utcnow
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.schemas.conversation import ConversationCreate, ConversationDetailOut, ConversationOut, ConversationUpdate
from app.schemas.message import MessageCreate, MessageOut
from app.services.platforms import send_reply

router = APIRouter()


# ── Conversations ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ConversationOut])
def list_conversations(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
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
    return query.order_by(Conversation.updated_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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
        source="rehearsal",
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
def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.status == "closed":
        raise HTTPException(status_code=400, detail="Cannot send messages to a closed conversation")

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if payload.message_type != "text":
        raise HTTPException(status_code=400, detail="Only text replies are supported")

    connection = None
    if conversation.platform_connection_id:
        connection = db.query(PlatformConnection).filter(
            PlatformConnection.id == conversation.platform_connection_id,
            PlatformConnection.user_id == current_user.id,
            PlatformConnection.is_active == True,
        ).first()
    else:
        # Backward compatibility for conversations created before the exact
        # channel was recorded. It is safe only when there is one candidate.
        candidates = db.query(PlatformConnection).filter(
            PlatformConnection.user_id == current_user.id,
            PlatformConnection.platform == conversation.platform,
            PlatformConnection.is_active == True,
        ).limit(2).all()
        if len(candidates) == 1:
            connection = candidates[0]
            conversation.platform_connection_id = connection.id
        elif len(candidates) > 1:
            raise HTTPException(
                status_code=409,
                detail="The channel for this conversation is ambiguous",
            )

    # A manually created rehearsal has no channel and remains a local note.
    # A real inbound thread has an exact connection and must be delivered.
    if connection:
        customer = db.query(Customer).filter(
            Customer.id == conversation.customer_id,
            Customer.user_id == current_user.id,
        ).first()
        if not customer or not customer.platform_id:
            raise HTTPException(status_code=400, detail="This customer is not connected to a channel")
        if not send_reply(connection.platform, connection.access_token,
                          customer.platform_id, content):
            raise HTTPException(status_code=502, detail="The channel did not accept the message")
    elif conversation.source == "channel":
        raise HTTPException(
            status_code=409,
            detail="The channel for this conversation is unavailable",
        )

    message = Message(
        conversation_id=conversation_id,
        sender_type="seller",
        message_type=payload.message_type,
        content=content,
    )
    db.add(message)
    conversation.updated_at = utcnow()
    db.commit()
    db.refresh(message)
    return message
