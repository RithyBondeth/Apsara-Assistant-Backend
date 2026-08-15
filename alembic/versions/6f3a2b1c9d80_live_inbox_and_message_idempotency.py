"""live inbox and message idempotency

Revision ID: 6f3a2b1c9d80
Revises: df4b77207c2e
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "6f3a2b1c9d80"
down_revision: Union[str, None] = "df4b77207c2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("platform_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_platform_connection_id",
        "conversations",
        "platform_connections",
        ["platform_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_conversations_platform_connection_id",
        "conversations",
        ["platform_connection_id"],
    )
    op.create_index(
        "uq_messages_conversation_external_id",
        "messages",
        ["conversation_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_conversation_external_id", table_name="messages")
    op.drop_index("ix_conversations_platform_connection_id", table_name="conversations")
    op.drop_constraint(
        "fk_conversations_platform_connection_id", "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "platform_connection_id")
