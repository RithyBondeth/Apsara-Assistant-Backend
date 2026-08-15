from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

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
    payment_qr_url: str | None = None

    @field_validator("payment_qr_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        """Reject anything Messenger and Telegram could not fetch.

        Both platforms download the image from this URL themselves, so a
        relative path or a data: URI fails at the moment a customer asks to
        pay — long after the seller has left the settings page. An empty
        string is how the form clears the field.
        """
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("Must be a full http:// or https:// image link")
        return value


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    business_name: str | None
    currency: str
    payment_qr_url: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: str
