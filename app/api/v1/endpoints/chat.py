from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.clock import utcnow
from app.database import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.product import Product
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, OrderDraftItemOut, OrderDraftOut
from app.schemas.message import MessageOut
from app.services.quota import spend_draft, spend_reply
from app.services.ai_service import (
    CATALOGUE_LIMIT,
    HISTORY_LIMIT,
    AIError,
    build_openai_messages,
    build_order_draft_messages,
    build_system_prompt,
    generate_ai_reply,
    generate_order_draft,
    payment_qr_message,
    split_payment_qr,
)

router = APIRouter()


@router.post("/{conversation_id}/order-draft", response_model=OrderDraftOut)
def draft_order(
    conversation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Extract a reviewable proposal; never create or reserve an order."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id,
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    history.reverse()
    if not any(message.content for message in history):
        raise HTTPException(status_code=400, detail="The conversation has no text to draft from")

    products = (
        db.query(Product)
        .filter(Product.user_id == current_user.id, Product.is_active == True)
        .order_by(Product.created_at.desc())
        .limit(CATALOGUE_LIMIT)
        .all()
    )
    if not products:
        raise HTTPException(status_code=400, detail="Add an active product before drafting an order")
    if not spend_draft(db, current_user.id):
        raise HTTPException(status_code=429, detail="You have reached today's AI order draft limit")

    try:
        extracted = generate_order_draft(build_order_draft_messages(products, history))
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    by_id = {product.id: product for product in products}
    quantities: dict[UUID, int] = {}
    warnings: list[str] = []
    for item in extracted.items:
        product = by_id.get(item.product_id)
        if product is None:
            warnings.append("The AI referenced a product outside the active catalogue; it was removed.")
            continue
        quantities[product.id] = quantities.get(product.id, 0) + item.quantity

    items: list[OrderDraftItemOut] = []
    for product_id, quantity in quantities.items():
        product = by_id[product_id]
        if quantity > product.stock:
            warnings.append(
                f"{product.name}: requested {quantity}, but only {product.stock} are in stock."
            )
        items.append(OrderDraftItemOut(
            product_id=product.id,
            product_name=product.name,
            quantity=quantity,
            unit_price=product.price,
            subtotal=product.price * quantity,
            stock=product.stock,
        ))

    missing_fields: list[str] = []
    if not items:
        missing_fields.append("items")
    if not extracted.delivery_address:
        missing_fields.append("delivery address")

    return OrderDraftOut(
        customer_id=conversation.customer_id,
        conversation_id=conversation.id,
        delivery_address=extracted.delivery_address,
        notes=extracted.notes,
        items=items,
        missing_fields=missing_fields,
        warnings=warnings,
    )


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
    conversation.updated_at = utcnow()
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
    # Same ceiling the webhooks are held to, so a seller cannot sidestep it by
    # driving the assistant from the dashboard instead.
    if not spend_reply(db, current_user.id):
        raise HTTPException(
            status_code=429,
            detail="You have reached today's limit for assistant replies. "
                   "Your message was saved.",
        )

    system_prompt = build_system_prompt(current_user, products)
    try:
        raw_reply = generate_ai_reply(build_openai_messages(system_prompt, history))
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ── 6. Save the AI reply, and the QR if it asked for one ─────────────────
    # There is no platform to hand the image to here, so the QR is recorded as
    # the message the customer would have received — this endpoint exists to
    # show the seller what their assistant does.
    reply_text, wants_qr = split_payment_qr(raw_reply)
    ai_msg = Message(
        conversation_id=conversation_id,
        sender_type="assistant",
        message_type="text",
        content=reply_text,
    )
    db.add(ai_msg)

    qr_msg = None
    if wants_qr and current_user.payment_qr_url:
        qr_msg = payment_qr_message(conversation_id, current_user.payment_qr_url)
        db.add(qr_msg)

    conversation.updated_at = utcnow()
    db.commit()
    db.refresh(ai_msg)
    if qr_msg is not None:
        db.refresh(qr_msg)

    return ChatResponse(
        customer_message=MessageOut.model_validate(customer_msg),
        ai_message=MessageOut.model_validate(ai_msg),
        qr_message=MessageOut.model_validate(qr_msg) if qr_msg else None,
    )
