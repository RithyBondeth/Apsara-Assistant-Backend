from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.currency import TCurrency
from app.schemas.auth import PASSWORD_MIN_LENGTH

# Each shop-profile field goes into the assistant's prompt on every reply, so
# it is bounded — generously for a delivery table, but not a novel.
SHOP_TEXT_MAX = 2000


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
    # Riel per dollar. Bounded so a typo cannot make every riel receipt a
    # mismatch — or a match.
    khr_rate: Decimal | None = Field(default=None, gt=1000, lt=20000)
    payment_qr_url: str | None = None
    shop_address: str | None = Field(default=None, max_length=SHOP_TEXT_MAX)
    shop_hours: str | None = Field(default=None, max_length=SHOP_TEXT_MAX)
    delivery_info: str | None = Field(default=None, max_length=SHOP_TEXT_MAX)
    shop_policies: str | None = Field(default=None, max_length=SHOP_TEXT_MAX)
    language: Literal["en", "km"] | None = None
    # Hours before the assistant resumes a thread the seller replied to.
    # 0 keeps it quiet until they hand the thread back by hand.
    manual_timeout_hours: int | None = Field(default=None, ge=0, le=720)
    low_stock_email_enabled: bool | None = None
    low_stock_telegram_enabled: bool | None = None
    attention_telegram_enabled: bool | None = None
    payment_telegram_enabled: bool | None = None
    customer_updates_enabled: bool | None = None
    khqr_enabled: bool | None = None
    bakong_account_id: str | None = Field(default=None, max_length=64)
    khqr_merchant_name: str | None = Field(default=None, max_length=25)
    khqr_merchant_city: str | None = Field(default=None, max_length=15)
    khqr_merchant_id: str | None = Field(default=None, max_length=32)
    khqr_acquiring_bank: str | None = Field(default=None, max_length=32)
    # Write-only: stored encrypted, never returned. An empty string clears it.
    bakong_api_token: str | None = Field(default=None, max_length=4000)

    @field_validator("bakong_account_id")
    @classmethod
    def _bakong_account(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("A Bakong account ID looks like yourname@bank")
        return value

    @field_validator("khqr_merchant_name", "khqr_merchant_city")
    @classmethod
    def _latin(cls, value: str | None) -> str | None:
        """Bank apps show these from Latin text; Khmer would arrive blank."""
        if value is None or not value.strip():
            return None
        if any(not (32 <= ord(ch) < 127) for ch in value):
            raise ValueError("Use Latin letters — bank apps cannot show Khmer here")
        return value.strip()

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

    @field_validator("shop_address", "shop_hours", "delivery_info", "shop_policies")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        """An emptied textarea clears the field rather than storing whitespace
        the prompt would then render as a heading with nothing under it."""
        if value is None:
            return None
        value = value.strip()
        return value or None


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    business_name: str | None
    currency: str
    khr_rate: Decimal
    payment_qr_url: str | None
    shop_address: str | None
    shop_hours: str | None
    delivery_info: str | None
    shop_policies: str | None
    language: str
    manual_timeout_hours: int
    low_stock_email_enabled: bool
    low_stock_telegram_enabled: bool
    attention_telegram_enabled: bool
    payment_telegram_enabled: bool
    customer_updates_enabled: bool = True
    khqr_enabled: bool = False
    bakong_account_id: str | None = None
    khqr_merchant_name: str | None = None
    khqr_merchant_city: str | None = None
    khqr_merchant_id: str | None = None
    khqr_acquiring_bank: str | None = None
    bakong_token_set: bool = False
    # Who is signed in: the owner, or a staff member working this shop.
    role: str = "owner"
    email_verified_at: datetime | None = None
    member_id: UUID | None = None
    member_name: str | None = None
    member_email: str | None = None
    telegram_chat_id: str | None
    telegram_chat_name: str | None
    telegram_linked_at: datetime | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TelegramLinkOut(BaseModel):
    """What the seller needs to link their Telegram: a link to open, and the
    same thing spelled out for anyone who would rather type it."""

    bot_username: str
    link_url: str
    code: str
    expires_in_minutes: int


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: str
