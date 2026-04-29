from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ConversationCreate(BaseModel):
    customer_id: UUID
    platform: str


class ConversationUpdate(BaseModel):
    status: str | None = None


class ConversationOut(BaseModel):
    id: UUID
    user_id: UUID
    customer_id: UUID
    platform: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
