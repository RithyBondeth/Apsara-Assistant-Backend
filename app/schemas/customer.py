from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CustomerCreate(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    platform: str | None = None
    platform_id: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None


class CustomerOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    phone: str | None
    email: str | None
    platform: str | None
    platform_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
