from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core import errors
from app.core.platforms import SUPPORTED_PLATFORMS, platform_list
from app.core.search import search_clause
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
from app.schemas.pagination import LimitParam, Page, SkipParam, paginate
from app.services import outbound

router = APIRouter()

# How much of a thread the detail endpoint returns. Enough that a seller opening
# a conversation sees the exchange in progress without paging, small enough that
# a thread running for months can't turn one dashboard click into a huge query.
MESSAGE_WINDOW = 30


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

@router.get("/", response_model=Page[ConversationOut])
def list_conversations(
    skip: int = SkipParam(),
    limit: int = LimitParam(),
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None),
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

    # A thread has no title, so the only thing a seller can search it by is who
    # they were talking to. Joined only when searching — an unconditional join
    # would hide threads whose customer row has gone.
    match = search_clause(search or "", Customer.name, Customer.platform_id)
    if match is not None:
        query = query.join(
            Customer, Conversation.customer_id == Customer.id
        ).filter(match)

    items, total = paginate(query.order_by(Conversation.updated_at.desc()), skip, limit)
    return Page(items=items, total=total)


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
        raise errors.conversation_not_found()

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
        raise errors.unsupported_platform(platform_list())

    customer = db.query(Customer).filter(
        Customer.id == payload.customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise errors.customer_not_found()

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
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conversation:
        raise errors.conversation_not_found()

    # Take the NEWEST window by sorting descending, then flip back to
    # chronological for display — ordering ascending with a limit would return
    # the oldest messages, i.e. the wrong end of the thread.
    recent = (
        db.query(Message)
        .options(joinedload(Message.attachments))
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(MESSAGE_WINDOW)
        .all()
    )
    total = (
        db.query(Message).filter(Message.conversation_id == conversation_id).count()
    )

    # Serialise the conversation WITHOUT the messages relationship — validating
    # the ORM object directly would lazy-load every message and undo the window.
    return ConversationDetailOut(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=[MessageOut.model_validate(m) for m in reversed(recent)],
        message_total=total,
    )


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
        raise errors.conversation_not_found()

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(conversation, field, value)
    db.commit()
    db.refresh(conversation)
    return conversation


# NOTE: there is deliberately no DELETE for a conversation.
#
# Deleting one CASCADEs every Message and Attachment in the thread and SET NULLs
# `conversation_id` on any Order that came out of it — so it destroys the
# customer's history *and* silently breaks the order→chat links, with no undo.
# No UI ever called it, so removing it costs nothing today.
#
# `status` (open/pending/closed) is the archival action sellers actually want,
# and it is already wired up. If a real delete is ever needed it should be a
# soft delete, so the orders that reference the thread keep resolving.


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/{conversation_id}/messages", response_model=Page[MessageOut])
def list_messages(
    conversation_id: UUID,
    skip: int = SkipParam(),
    limit: int = LimitParam(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id, Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise errors.conversation_not_found()

    query = (
        db.query(Message)
        .options(joinedload(Message.attachments))
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    items, total = paginate(query, skip, limit)
    return Page(items=items, total=total)


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
        raise errors.conversation_not_found()

    if conversation.status == "closed":
        raise errors.conversation_closed()

    content = (payload.content or "").strip()
    if not content:
        raise errors.message_empty()

    customer = db.query(Customer).filter(Customer.id == conversation.customer_id).first()
    if not customer:
        raise errors.customer_not_found()

    try:
        await outbound.send_to_customer(db, conversation, customer, content)
    except outbound.OutboundError as exc:
        raise errors.delivery_failed(str(exc))

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
