"""scope external message ids to platform connections

Revision ID: 7a4d8e2f1b60
Revises: 6f3a2b1c9d80
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7a4d8e2f1b60"
down_revision: Union[str, None] = "6f3a2b1c9d80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("uq_messages_conversation_external_id", table_name="messages")
    op.add_column(
        "messages",
        sa.Column("platform_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_platform_connection_id",
        "messages",
        "platform_connections",
        ["platform_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_messages_platform_connection_id",
        "messages",
        ["platform_connection_id"],
    )
    op.create_index(
        "uq_messages_connection_external_id",
        "messages",
        ["platform_connection_id", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "platform_connection_id IS NOT NULL AND external_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_connection_external_id", table_name="messages")
    op.drop_index("ix_messages_platform_connection_id", table_name="messages")
    op.drop_constraint("fk_messages_platform_connection_id", "messages", type_="foreignkey")
    op.drop_column("messages", "platform_connection_id")
    op.create_index(
        "uq_messages_conversation_external_id",
        "messages",
        ["conversation_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
