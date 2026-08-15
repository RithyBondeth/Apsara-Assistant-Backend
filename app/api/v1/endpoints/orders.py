from __future__ import annotations

import logging
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.core.config import settings
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.order import PAID, PAYMENT_PENDING, UNPAID, Order
from app.models.order_item import OrderItem
from app.models.platform_connection import PlatformConnection
from app.models.product import Product
from app.models.user import User
from app.schemas.order import CheckoutOut, OrderCreate, OrderOut, OrderUpdate
from app.services import stripe_gateway
from app.services.platforms import STRIPE

logger = logging.getLogger(__name__)

router = APIRouter()

# Allowed order statuses (see app/models/order.py)
VALID_STATUSES = {"pending", "confirmed", "processing", "shipped", "delivered", "cancelled"}


def _restock_order(db: Session, order: Order) -> None:
    """Return each line item's quantity back to the product's stock."""
    for item in order.items:
        product = db.query(Product).filter(Product.id == item.product_id).with_for_update().first()
        if product is not None:
            product.stock += item.quantity


def _deduct_order(db: Session, order: Order) -> None:
    """Take each line item's quantity back out of stock.

    Used when a cancelled order is revived: cancelling returned the goods to
    inventory, so re-activating has to take them out again or the order ships
    stock the seller never gave up.
    """
    for item in order.items:
        product = db.query(Product).filter(Product.id == item.product_id).with_for_update().first()
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot reopen this order: a product on it no longer exists",
            )
        if product.stock < item.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reopen this order: insufficient stock for '{product.name}' "
                       f"(available: {product.stock}, needed: {item.quantity})",
            )
        product.stock -= item.quantity


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
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Order must contain at least one item"
        )

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
        total_amount=0,
        # Snapshot, not a lookup: if the seller later switches currency, this
        # order must keep the one it was actually priced and agreed in.
        currency=current_user.currency,
    )

    total = 0
    for item in payload.items:
        if item.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Item quantity must be positive"
            )

        # Locked for the transaction: checking stock and then decrementing it is
        # a read-then-write, and two concurrent orders would otherwise both pass
        # the check and oversell.
        product = db.query(Product).filter(
            Product.id == item.product_id, Product.user_id == current_user.id
        ).with_for_update().first()
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {item.product_id} not found",
            )
        if not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{product.name}' is not available",
            )
        if product.stock < item.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for '{product.name}' (available: {product.stock})",
            )

        # Use the product's server-side price as the source of truth
        unit_price = product.price
        subtotal = unit_price * item.quantity
        total += subtotal
        product.stock -= item.quantity

        order.items.append(
            OrderItem(
                product_id=product.id,
                quantity=item.quantity,
                unit_price=unit_price,
                subtotal=subtotal,
            )
        )

    order.total_amount = total
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = db.query(Order).options(joinedload(Order.items)).filter(
        Order.id == order_id, Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


@router.patch("/{order_id}", response_model=OrderOut)
def update_order(
    order_id: UUID,
    payload: OrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = db.query(Order).options(joinedload(Order.items)).filter(
        Order.id == order_id, Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    data = payload.model_dump(exclude_unset=True)
    new_status = data.get("status")
    if new_status is not None and new_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
        )

    # Stock follows the cancelled/not-cancelled boundary in both directions
    if new_status is not None and new_status != order.status:
        if new_status == "cancelled":
            _restock_order(db, order)
        elif order.status == "cancelled":
            _deduct_order(db, order)

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
    db.commit()

    return CheckoutOut(checkout_url=session.url, session_id=session.id,
                       payment_status=order.payment_status)


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(
    order_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = db.query(Order).options(joinedload(Order.items)).filter(
        Order.id == order_id, Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # A cancelled order has already had its stock returned; don't restock twice
    if order.status != "cancelled":
        _restock_order(db, order)

    db.delete(order)
    db.commit()
