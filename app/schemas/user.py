from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.currency import TCurrency
from app.schemas.auth import PASSWORD_MIN_LENGTH


class UserCreate(BaseModel):
    email: EmailStr
    # The web client already enforces this; without it the API would accept a
    # one-character password from any other caller.
    password: str = Field(min_length=PASSWORD_MIN_LENGTH)
    full_name: str
    business_name: str | None = None


class UserUpdate(BaseModel):
    full_name: str | None = None
    business_name: str | None = None
    currency: TCurrency | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    business_name: str | None
    currency: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: str
