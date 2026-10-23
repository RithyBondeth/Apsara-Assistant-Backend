from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.database import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_actor(
    bearer_token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=settings.AUTH_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    """The person signed in: the shop's owner, or one of its staff."""
    token = bearer_token or cookie_token
    user_id = decode_access_token(token) if token else None
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user


def get_current_user(
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
) -> User:
    """The shop the request acts for — the tenant every query is scoped to.

    For the owner that is their own row. For a staff member it is their
    owner's: staff see and work the same inbox, orders and catalogue, so
    every `user_id == current_user.id` filter already scopes them correctly.
    Who actually did something comes from `get_actor`.
    """
    if actor.shop_id is None:
        return actor
    owner = db.query(User).filter(User.id == actor.shop_id, User.is_active == True).first()
    if not owner:
        # A shop that was closed takes its staff's access with it.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return owner


def require_owner(actor: User = Depends(get_actor)) -> User:
    """For what only the shop's owner may change: its channels, payment
    details, settings and team."""
    if actor.shop_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the shop owner can do this")
    return actor
