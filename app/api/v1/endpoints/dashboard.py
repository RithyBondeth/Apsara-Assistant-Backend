from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.order import Order
from app.models.product import Product
from app.models.user import User
from app.schemas.dashboard import DashboardStats

router = APIRouter()


@router.get("/stats", response_model=DashboardStats)
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return headline counts + revenue for the current seller in one call.

    Uses SQL aggregates so the dashboard doesn't have to fetch whole tables to
    count them client-side.
    """
    uid = current_user.id

    products = (
        db.query(func.count(Product.id))
        .filter(Product.user_id == uid, Product.is_active == True)
        .scalar()
    )
    customers = (
        db.query(func.count(Customer.id)).filter(Customer.user_id == uid).scalar()
    )
    conversations = (
        db.query(func.count(Conversation.id)).filter(Conversation.user_id == uid).scalar()
    )
    open_conversations = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.user_id == uid, Conversation.status == "open")
        .scalar()
    )
    orders = db.query(func.count(Order.id)).filter(Order.user_id == uid).scalar()
    pending_orders = (
        db.query(func.count(Order.id))
        .filter(Order.user_id == uid, Order.status == "pending")
        .scalar()
    )
    revenue = (
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(Order.user_id == uid, Order.status != "cancelled")
        .scalar()
    )

    return DashboardStats(
        products=products or 0,
        customers=customers or 0,
        conversations=conversations or 0,
        open_conversations=open_conversations or 0,
        orders=orders or 0,
        pending_orders=pending_orders or 0,
        revenue=Decimal(revenue or 0),
    )
