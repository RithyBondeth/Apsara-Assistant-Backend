"""Who works a shop: its owner and their staff."""

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.user import User

OWNER = "owner"
STAFF = "staff"
MAX_STAFF = 20


def members(db: Session, shop_id, *, active_only: bool = False) -> list[User]:
    query = db.query(User).filter(or_(User.id == shop_id, User.shop_id == shop_id))
    if active_only:
        query = query.filter(User.is_active == True)  # noqa: E712
    # The owner first, then staff by when they joined.
    return sorted(query.all(), key=lambda u: (u.shop_id is not None, u.created_at))


def is_member(db: Session, shop_id, user_id) -> bool:
    return db.query(User.id).filter(
        User.id == user_id, User.is_active == True,  # noqa: E712
        or_(User.id == shop_id, User.shop_id == shop_id),
    ).first() is not None
