"""Deleting data on request: a shop closing its account, or a customer asking
Meta to have their data removed.

Most rows go by ON DELETE CASCADE from the user or the customer. Sales
records are the exception — returns, purchase orders and order lines point
at products with RESTRICT, so a product cannot vanish from under a sale — and
are removed first, leaf to root. Stored media follows its rows through the
storage tombstones (app/services/storage.py).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.operations import PurchaseOrder, SalesReturn
from app.models.order import Order
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.services.platforms import TELEGRAM, remove_telegram_webhook

logger = logging.getLogger(__name__)


def delete_shop(db: Session, owner: User) -> None:
    """Remove an owner's account, their staff, and everything in the shop.
    Committed by the caller."""
    # So Telegram stops delivering to a connection that no longer exists.
    for connection in db.query(PlatformConnection).filter(
            PlatformConnection.user_id == owner.id, PlatformConnection.platform == TELEGRAM):
        remove_telegram_webhook(connection.access_token)

    for model in (SalesReturn, PurchaseOrder, Order):
        db.query(model).filter(model.user_id == owner.id).delete(synchronize_session=False)
    # Core delete, not db.delete(): the ORM would try to null each child's
    # user_id itself rather than let the database cascade.
    db.query(User).filter(User.id == owner.id).delete(synchronize_session=False)
    logger.info("Deleted shop %s and its data", owner.id)


ANONYMOUS = "Deleted customer"


def erase_customer(db: Session, customer: Customer) -> None:
    """Remove what a customer said and who they are, keeping the shop's sales.

    Their conversations go — every message, photo and voice note with them.
    Their orders stay, because a shop's books are not the customer's to
    delete, but stop saying who placed them or where they were sent.
    """
    db.query(Conversation).filter(Conversation.customer_id == customer.id).delete(
        synchronize_session=False)
    db.query(Order).filter(Order.customer_id == customer.id).update(
        {Order.delivery_address: None, Order.notes: None}, synchronize_session=False)
    customer.name = ANONYMOUS
    customer.phone = None
    customer.email = None
    # Kept unique-looking but meaningless, so a new message from the same
    # person starts a new customer rather than reviving this one.
    customer.platform_id = f"deleted-{uuid.uuid4().hex}"
