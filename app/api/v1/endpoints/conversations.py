from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_current_user
from app.core.clock import utcnow
from app.database import get_db
from app.models.conversation import Conversation, ConversationNote, ConversationTag
from app.models.customer import Customer
from app.models.message import Message
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationNoteCreate,
    ConversationNoteOut,
    ConversationOut,
    ConversationTagCreate,
    ConversationTagOut,
    ConversationUpdate,
    InboxMetricsOut,
)
from app.schemas.message import MessageCreate, MessageOut
from app.services.platforms import send_reply

router = APIRouter()


def _attach_message_previews(db: Session, conversations: list[Conversation]) -> None:
    """Attach the newest message with one bounded query for inbox list rows."""
    if not conversations:
        return
    ids = [conversation.id for conversation in conversations]
    ranked = (
        db.query(
            Message.conversation_id.label("conversation_id"),
            Message.content.label("content"),
            Message.sender_type.label("sender_type"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(Message.created_at.desc(), Message.id.desc()),
            ).label("position"),
        )
        .filter(Message.conversation_id.in_(ids))
        .subquery()
    )
    newest = db.query(ranked).filter(ranked.c.position == 1).all()
    by_conversation = {message.conversation_id: message for message in newest}
    for conversation in conversations:
        message = by_conversation.get(conversation.id)
        conversation.last_message_preview = message.content[:120] if message and message.content else None
        conversation.last_message_sender = message.sender_type if message else None


# ── Conversations ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ConversationOut])
def list_conversations(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    unread_only: bool = Query(default=False),
    handling_mode: str | None = Query(default=None),
    assignment: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        db.query(Conversation)
        .options(selectinload(Conversation.tags))
        .filter(Conversation.user_id == current_user.id)
    )
    if status:
        query = query.filter(Conversation.status == status)
    if platform:
        query = query.filter(Conversation.platform == platform)
    if customer_id:
        query = query.filter(Conversation.customer_id == customer_id)
    if unread_only:
        query = query.filter(Conversation.unread_count > 0)
    if handling_mode:
        if handling_mode not in {"auto", "manual"}:
            raise HTTPException(status_code=422, detail="handling_mode must be auto or manual")
        query = query.filter(Conversation.handling_mode == handling_mode)
    if assignment == "me":
        query = query.filter(Conversation.assigned_user_id == current_user.id)
    elif assignment == "unassigned":
        query = query.filter(Conversation.assigned_user_id.is_(None))
    elif assignment:
        raise HTTPException(status_code=422, detail="assignment must be me or unassigned")
    if tag:
        query = query.filter(Conversation.tags.any(ConversationTag.name == tag.strip().lower()))
    if search and search.strip():
        needle = f"%{search.strip()}%"
        query = query.join(Customer).filter(or_(Customer.name.ilike(needle), Customer.phone.ilike(needle)))
    conversations = query.order_by(Conversation.updated_at.desc()).offset(skip).limit(limit).all()
    _attach_message_previews(db, conversations)
    return conversations


@router.get("/metrics", response_model=InboxMetricsOut)
def inbox_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    valid_response = (
        Conversation.first_response_at.isnot(None)
        & Conversation.first_customer_message_at.isnot(None)
        & (Conversation.first_response_at >= Conversation.first_customer_message_at)
    )
    metrics = (
        db.query(
            func.count(Conversation.id).label("total"),
            func.count(Conversation.id).filter(Conversation.status == "open").label("open"),
            func.count(Conversation.id).filter(Conversation.status == "pending").label("pending"),
            func.count(Conversation.id).filter(Conversation.status == "closed").label("closed"),
            func.coalesce(func.sum(Conversation.unread_count), 0).label("unread"),
            func.count(Conversation.id).filter(
                Conversation.handling_mode == "manual"
            ).label("manual"),
            func.count(Conversation.id).filter(
                Conversation.assigned_user_id.is_(None)
            ).label("unassigned"),
            func.avg(
                func.extract(
                    "epoch",
                    Conversation.first_response_at - Conversation.first_customer_message_at,
                )
            ).filter(valid_response).label("average_first_response_seconds"),
        )
        .filter(
            Conversation.user_id == current_user.id,
            Conversation.source == "channel",
        )
        .one()
    )
    return InboxMetricsOut(
        total=metrics.total,
        open=metrics.open,
        pending=metrics.pending,
        closed=metrics.closed,
        unread=metrics.unread,
        manual=metrics.manual,
        unassigned=metrics.unassigned,
        average_first_response_seconds=(
            float(metrics.average_first_response_seconds)
            if metrics.average_first_response_seconds is not None
            else None
        ),
    )


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
        _attach_message_previews(db, [existing])
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
    _attach_message_previews(db, [conversation])
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = (
        db.query(Conversation)
        .options(
            joinedload(Conversation.messages).joinedload(Message.attachments),
            selectinload(Conversation.notes),
            selectinload(Conversation.tags),
        )
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    newest = max(
        conversation.messages,
        key=lambda message: (message.created_at, str(message.id)),
        default=None,
    )
    conversation.last_message_preview = newest.content[:120] if newest and newest.content else None
    conversation.last_message_sender = newest.sender_type if newest else None
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

    changes = payload.model_dump(exclude_unset=True)
    assignee = changes.get("assigned_user_id")
    if assignee is not None and assignee != current_user.id:
        # The current account is the only staff identity today. Keeping this a
        # foreign key makes team members additive later without unsafe IDs now.
        raise HTTPException(status_code=404, detail="Team member not found")
    for field, value in changes.items():
        setattr(conversation, field, value)
    if changes.get("handling_mode") == "manual" and "assigned_user_id" not in changes:
        conversation.assigned_user_id = current_user.id
    elif changes.get("handling_mode") == "auto" and "assigned_user_id" not in changes:
        conversation.assigned_user_id = None
    db.commit()
    db.refresh(conversation)
    _attach_message_previews(db, [conversation])
    return conversation


@router.post("/{conversation_id}/read", response_model=ConversationOut)
def mark_conversation_read(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.unread_count = 0
    conversation.last_read_at = utcnow()
    db.commit()
    db.refresh(conversation)
    _attach_message_previews(db, [conversation])
    return conversation


@router.post("/{conversation_id}/notes", response_model=ConversationNoteOut, status_code=201)
def add_conversation_note(
    conversation_id: UUID,
    payload: ConversationNoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    note = ConversationNote(
        conversation_id=conversation.id,
        author_user_id=current_user.id,
        content=payload.content.strip(),
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.delete("/{conversation_id}/notes/{note_id}", status_code=204)
def delete_conversation_note(
    conversation_id: UUID,
    note_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = (
        db.query(ConversationNote)
        .join(Conversation)
        .filter(
            ConversationNote.id == note_id,
            ConversationNote.conversation_id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete(note)
    db.commit()


@router.post("/{conversation_id}/tags", response_model=ConversationTagOut, status_code=201)
def add_conversation_tag(
    conversation_id: UUID,
    payload: ConversationTagCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    name = payload.name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="Tag cannot be empty")
    existing = db.query(ConversationTag).filter(
        ConversationTag.conversation_id == conversation.id,
        ConversationTag.name == name,
    ).first()
    if existing:
        return existing
    tag = ConversationTag(conversation_id=conversation.id, name=name)
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        # Two staff actions can add the same tag at once. The unique key is the
        # final arbiter; return the winner instead of leaking a 500.
        db.rollback()
        existing = db.query(ConversationTag).filter(
            ConversationTag.conversation_id == conversation.id,
            ConversationTag.name == name,
        ).first()
        if existing:
            return existing
        raise
    db.refresh(tag)
    return tag


@router.delete("/{conversation_id}/tags/{tag_id}", status_code=204)
def delete_conversation_tag(
    conversation_id: UUID,
    tag_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tag = (
        db.query(ConversationTag)
        .join(Conversation)
        .filter(
            ConversationTag.id == tag_id,
            ConversationTag.conversation_id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    db.delete(tag)
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
    now = utcnow()
    conversation.updated_at = now
    conversation.last_seller_message_at = now
    if conversation.first_response_at is None and conversation.first_customer_message_at:
        conversation.first_response_at = now
    conversation.handling_mode = "manual"
    conversation.assigned_user_id = current_user.id
    db.commit()
    db.refresh(message)
    return message
