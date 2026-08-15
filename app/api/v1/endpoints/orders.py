from __future__ import annotations

import logging
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core.clock import utcnow
from app.core.config import settings
from app.database import get_db
from app.models.conversation import Conversation
from app.models.attachment import Attachment
from app.models.customer import Customer
from app.models.order import PAID, PAYMENT_PENDING, UNPAID, Order
from app.models.order_item import OrderItem
from app.models.message import Message
from app.models.platform_connection import PlatformConnection
from app.models.product import Product
from app.models.user import User
from app.schemas.message import AttachmentOut
from app.schemas.order import CheckoutOut, OrderCreate, OrderOut, OrderUpdate
from app.services import stripe_gateway
from app.services.inventory import (
    FULFILLED_STATUSES,
    RESERVING_STATUSES,
    lock_order_products,
    move_stock,
    release_expired_reservations,
    release_order_stock,
    reservation_deadline,
)
from app.services.platforms import STRIPE

logger = logging.getLogger(__name__)

router = APIRouter()

# Allowed order statuses (see app/models/order.py)
VALID_STATUSES = {"pending", "confirmed", "processing", "shipped", "delivered", "cancelled"}


def _owned_order(db: Session, order_id: UUID, user_id: UUID, *, lock: bool = False) -> Order:
    query = db.query(Order).filter(Order.id == order_id, Order.user_id == user_id)
    if not lock:
        query = query.options(joinedload(Order.items))
    order = query.with_for_update().first() if lock else query.first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


def _receipt_for_order(db: Session, order: Order, attachment_id: UUID,
                       *, lock: bool = False) -> Attachment:
    query = (
        db.query(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .filter(
            Attachment.id == attachment_id,
            Message.conversation_id == order.conversation_id,
            Message.sender_type == "customer",
            Attachment.blob.isnot(None),
        )
    )
    receipt = query.with_for_update().first() if lock else query.first()
    if not receipt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Receipt not found for this order conversation")
    return receipt


def _deduct_order(
    db: Session, order: Order, *, reserve: bool, actor_user_id: UUID | None = None
) -> None:
    """Take each line item's quantity back out of stock.

    Used when a cancelled order is revived: cancelling returned the goods to
    inventory, so re-activating has to take them out again or the order ships
    stock the seller never gave up.
    """
    products = lock_order_products(db, order)
    for item in order.items:
        product = products.get(item.product_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot reopen this order: a product on it no longer exists",
            )
        move_stock(
            db,
            product,
            available_delta=-item.quantity,
            reserved_delta=item.quantity if reserve else 0,
            kind="reservation_reopened" if reserve else "order_reopened",
            reason=f"Order {order.id} reopened",
            order_id=order.id,
            actor_user_id=actor_user_id,
        )


def _change_reserved_stock(
    db: Session, order: Order, *, delta_sign: int, kind: str, actor_user_id: UUID
) -> None:
    products = lock_order_products(db, order)
    for item in order.items:
        product = products.get(item.product_id)
        if product is not None:
            move_stock(
                db,
                product,
                available_delta=0,
                reserved_delta=delta_sign * item.quantity,
                kind=kind,
                reason=f"Order {order.id} status changed",
                order_id=order.id,
                actor_user_id=actor_user_id,
            )


@router.get("/", response_model=list[OrderOut])
def list_orders(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    status: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Order).options(joinedload(Order.items)).filter(Order.user_id == current_user.id)
    if status:
        query = query.filter(Order.status == status)
    if customer_id:
        query = query.filter(Order.customer_id == customer_id)
    return query.order_by(Order.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Expired pending reservations must not make a sellable item appear
    # unavailable when the next customer orders it.
    release_expired_reservations(db, current_user.id)
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Order must contain at least one item"
        )

    quantities: dict[UUID, int] = {}
    for item in payload.items:
        if item.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Item quantity must be positive"
            )
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

    # Verify the customer belongs to the seller
    customer = db.query(Customer).filter(
        Customer.id == payload.customer_id, Customer.user_id == current_user.id
    ).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # Verify the conversation (if provided) belongs to the seller
    if payload.conversation_id is not None:
        conversation = db.query(Conversation).filter(
            Conversation.id == payload.conversation_id, Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    order = Order(
        user_id=current_user.id,
        customer_id=payload.customer_id,
        conversation_id=payload.conversation_id,
        delivery_address=payload.delivery_address,
        notes=payload.notes,
        status="pending",
        reservation_expires_at=reservation_deadline(),
        total_amount=0,
        # Snapshot, not a lookup: if the seller later switches currency, this
        # order must keep the one it was actually priced and agreed in.
        currency=current_user.currency,
    )
    db.add(order)
    db.flush()

    total = 0
    product_ids = sorted(quantities, key=str)
    locked_products = (
        db.query(Product)
        .filter(Product.id.in_(product_ids), Product.user_id == current_user.id)
        .order_by(Product.id)
        .with_for_update()
        .all()
    )
    products_by_id = {product.id: product for product in locked_products}

    for product_id in product_ids:
        quantity = quantities[product_id]
        # Locked for the transaction: checking stock and then decrementing it is
        # a read-then-write. Stable lock order also prevents two multi-product
        # orders from deadlocking when their lines arrive in opposite order.
        product = products_by_id.get(product_id)
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )
        if not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{product.name}' is not available",
            )
        # Use the product's server-side price as the source of truth
        unit_price = product.price
        subtotal = unit_price * quantity
        total += subtotal
        move_stock(
            db,
            product,
            available_delta=-quantity,
            reserved_delta=quantity,
            kind="reservation_created",
            reason=f"Reserved for order {order.id}",
            order_id=order.id,
        )

        order.items.append(
            OrderItem(
                product_id=product.id,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=subtotal,
            )
        )

    order.total_amount = total
    db.commit()
    db.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id)
    return order


@router.patch("/{order_id}", response_model=OrderOut)
def update_order(
    order_id: UUID,
    payload: OrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id, lock=True)

    data = payload.model_dump(exclude_unset=True)
    new_status = data.get("status")
    if new_status is not None and new_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )

    # Stock follows both the cancelled boundary and the reserved/fulfilled
    # boundary, so the available balance and reserved subtotal remain distinct.
    if new_status is not None and new_status != order.status:
        if new_status == "cancelled":
            release_order_stock(
                db, order, kind="order_cancelled", actor_user_id=current_user.id
            )
        elif order.status == "cancelled":
            _deduct_order(
                db,
                order,
                reserve=new_status in RESERVING_STATUSES,
                actor_user_id=current_user.id,
            )
        elif order.status in RESERVING_STATUSES and new_status in FULFILLED_STATUSES:
            _change_reserved_stock(
                db, order, delta_sign=-1, kind="reservation_fulfilled",
                actor_user_id=current_user.id,
            )
        elif order.status in FULFILLED_STATUSES and new_status in RESERVING_STATUSES:
            _change_reserved_stock(
                db, order, delta_sign=1, kind="reservation_restored",
                actor_user_id=current_user.id,
            )

        if new_status == "pending":
            order.reservation_expires_at = reservation_deadline()
        elif new_status != "cancelled":
            order.reservation_expires_at = None

    for field, value in data.items():
        setattr(order, field, value)
    db.commit()
    db.refresh(order)
    return order


@router.post("/{order_id}/checkout", response_model=CheckoutOut)
def create_checkout(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Open a Stripe payment page for this order and return its link.

    Reissuing is allowed and deliberate: Checkout Sessions expire after 24
    hours, and a customer who takes a day to pay should not need the order
    rebuilt. The newest session id replaces the old one, so a payment on a
    stale link still resolves — the webhook matches on the order id in the
    session's metadata, not on whichever session happens to be current.
    """
    order = db.query(Order).options(joinedload(Order.items)).filter(
        Order.id == order_id, Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.payment_status == PAID:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This order has already been paid.")
    if order.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This order is cancelled. Reopen it before taking payment.")
    if order.total_amount is None or order.total_amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This order has nothing to charge for.")

    connection = (
        db.query(PlatformConnection)
        .filter(PlatformConnection.user_id == current_user.id,
                PlatformConnection.platform == STRIPE,
                PlatformConnection.is_active == True)
        .first()
    )
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect Stripe under Integrations before taking card payments.",
        )

    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    base = settings.APP_BASE_URL.rstrip("/")
    try:
        session = stripe_gateway.create_checkout_session(
            connection.access_token,
            order_id=order.id,
            amount=order.total_amount,
            currency=order.currency,
            description=f"Order from {current_user.business_name or current_user.full_name}",
            success_url=f"{base}/pay/success?order={order.id}",
            cancel_url=f"{base}/pay/cancelled?order={order.id}",
            customer_email=customer.email if customer else None,
        )
    except stripe.StripeError as exc:
        # Surfaced rather than swallowed: "your card page could not be created"
        # is something the seller can act on, usually by fixing their account.
        logger.warning("Stripe checkout failed for order %s: %s", order.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Stripe could not create the payment page: {exc.user_message or exc}")

    order.stripe_session_id = session.id
    # Not "paid" — only the webhook may say that. This records that a payment
    # page is outstanding, which is what the seller sees while waiting.
    if order.payment_status == UNPAID:
        order.payment_status = PAYMENT_PENDING
    if order.status == "pending":
        # A newly issued checkout is a live customer commitment; keep its
        # reservation for the lifetime of the fresh payment link.
        order.reservation_expires_at = reservation_deadline()
    db.commit()

    return CheckoutOut(checkout_url=session.url, session_id=session.id,
                       payment_status=order.payment_status)


@router.get("/{order_id}/receipts", response_model=list[AttachmentOut])
def list_order_receipts(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id)
    if order.conversation_id is None:
        return []
    return (
        db.query(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .filter(
            Message.conversation_id == order.conversation_id,
            Message.sender_type == "customer",
            Attachment.blob.isnot(None),
        )
        .order_by(Attachment.created_at.desc())
        .all()
    )


@router.post("/{order_id}/receipts/{attachment_id}/confirm", response_model=OrderOut)
def confirm_order_receipt(
    order_id: UUID,
    attachment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id, lock=True)
    if order.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Reopen this order before confirming payment")
    receipt = _receipt_for_order(db, order, attachment_id, lock=True)

    if order.payment_status == PAID:
        if order.payment_receipt_attachment_id == receipt.id:
            return order
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This order already has a confirmed payment")
    used = db.query(Order.id).filter(
        Order.payment_receipt_attachment_id == receipt.id,
        Order.id != order.id,
    ).first()
    if used:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This receipt is already attached to another order")

    now = utcnow()
    receipt.review_status = "accepted"
    receipt.reviewed_at = now
    receipt.reviewed_by_user_id = current_user.id
    order.payment_status = PAID
    order.payment_method = "qr"
    order.payment_receipt_attachment_id = receipt.id
    order.payment_confirmed_by_user_id = current_user.id
    order.paid_at = now
    db.commit()
    db.refresh(order)
    return order


@router.post("/{order_id}/receipts/{attachment_id}/reject", response_model=AttachmentOut)
def reject_order_receipt(
    order_id: UUID,
    attachment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id, lock=True)
    receipt = _receipt_for_order(db, order, attachment_id, lock=True)
    if order.payment_receipt_attachment_id == receipt.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A confirmed payment receipt cannot be rejected")
    receipt.review_status = "rejected"
    receipt.reviewed_at = utcnow()
    receipt.reviewed_by_user_id = current_user.id
    db.commit()
    db.refresh(receipt)
    return receipt


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = _owned_order(db, order_id, current_user.id, lock=True)

    # A cancelled order has already had its stock returned; don't restock twice.
    if order.status != "cancelled":
        release_order_stock(
            db, order, kind="order_deleted", actor_user_id=current_user.id
        )

    db.delete(order)
    db.commit()
