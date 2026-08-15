from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.currency import TCurrency


class PaymentQrUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    bank_name: str | None = Field(default=None, max_length=100)
    account_name: str | None = Field(default=None, max_length=100)
    currency: TCurrency | None = None
    is_active: bool | None = None
    is_default: bool | None = None

    @field_validator("name")
    @classmethod
    def name_must_have_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Name cannot be empty")
        return value

    @model_validator(mode="after")
    def default_must_be_active(self):
        if self.is_default is True and self.is_active is False:
            raise ValueError("The default QR must be active")
        return self


class PaymentQrOut(BaseModel):
    id: UUID
    name: str
    bank_name: str | None
    account_name: str | None
    currency: str | None
    url: str
    file_name: str
    file_size: int
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
