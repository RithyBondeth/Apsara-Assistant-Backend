import uuid

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core import errors
from app.core.security import decode_access_token
from app.database import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    subject = decode_access_token(token)
    if not subject:
        raise errors.invalid_token()

    try:
        user_id = uuid.UUID(str(subject))
    except (ValueError, TypeError):
        raise errors.invalid_token()

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise errors.user_not_found()

    return user
