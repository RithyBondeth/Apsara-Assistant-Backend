from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderCreate, OrderOut, OrderUpdate

router = APIRouter()

# Allowed order statuses (see app/models/order.py)
VALID_STATUSES = {"pending", "confirmed", "processing", "shipped", "delivered", "cancelled"}


def _restock_order(db: Session, order: Order) -> None:
    """Return each line item's quantity back to the product's stock.

    Rows are locked (FOR UPDATE) in id order to stay consistent with
    create_order and avoid lost updates / deadlocks under concurrent cancels.
    """
    quantities: dict = {}
    for item in order.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

    locked = (
        db.query(Product)
        .filter(Product.id.in_(quantities.keys()))
        .order_by(Product.id)
        .with_for_update()
        .all()
    )
    for product in locked:
        product.stock += quantities[product.id]


@router.get("/", response_model=list[OrderOut])
def list_orders(
    skip: int = 0,
    limit: int = 50,
    status: str | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    conversation_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Order).options(joinedload(Order.items)).filter(Order.user_id == current_user.id)
    if status:
        query = query.filter(Order.status == status)
    if customer_id:
        query = query.filter(Order.customer_id == customer_id)
    if conversation_id:
        # Lets the chat panel show the orders that came out of this thread.
        query = query.filter(Order.conversation_id == conversation_id)
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
    )

    if any(item.quantity <= 0 for item in payload.items):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Item quantity must be positive"
        )

    # Lock every product row up front (FOR UPDATE) so concurrent orders can't
    # oversell the same stock. Locking in a deterministic id order avoids
    # deadlocks between two orders that share products.
    product_ids = sorted({item.product_id for item in payload.items})
    locked = (
        db.query(Product)
        .filter(Product.id.in_(product_ids), Product.user_id == current_user.id)
        .order_by(Product.id)
        .with_for_update()
        .all()
    )
    products_by_id = {p.id: p for p in locked}

    total = 0
    for item in payload.items:
        product = products_by_id.get(item.product_id)
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

    # Return stock to inventory when an order transitions into "cancelled"
    if new_status == "cancelled" and order.status != "cancelled":
        _restock_order(db, order)

    for field, value in data.items():
        setattr(order, field, value)
    db.commit()
    db.refresh(order)
    return order


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
