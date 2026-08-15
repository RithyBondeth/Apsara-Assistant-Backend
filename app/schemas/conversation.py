from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.message import MessageOut


class ConversationCreate(BaseModel):
    customer_id: UUID
    platform: str


class ConversationUpdate(BaseModel):
    status: Literal["open", "pending", "closed"] | None = None
    handling_mode: Literal["auto", "manual"] | None = None
    assigned_user_id: UUID | None = None


class ConversationNoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ConversationNoteOut(BaseModel):
    id: UUID
    conversation_id: UUID
    author_user_id: UUID | None
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class ConversationTagOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: UUID
    user_id: UUID
    customer_id: UUID
    platform_connection_id: UUID | None
    platform: str
    source: str
    status: str
    handling_mode: str
    assigned_user_id: UUID | None
    unread_count: int
    last_read_at: datetime | None
    first_customer_message_at: datetime | None
    first_response_at: datetime | None
    last_customer_message_at: datetime | None
    last_seller_message_at: datetime | None
    last_message_preview: str | None = None
    last_message_sender: str | None = None
    created_at: datetime
    updated_at: datetime
    tags: list[ConversationTagOut] = []

    model_config = {"from_attributes": True}


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []
    notes: list[ConversationNoteOut] = []


class InboxMetricsOut(BaseModel):
    total: int
    open: int
    pending: int
    closed: int
    unread: int
    manual: int
    unassigned: int
    average_first_response_seconds: float | None
