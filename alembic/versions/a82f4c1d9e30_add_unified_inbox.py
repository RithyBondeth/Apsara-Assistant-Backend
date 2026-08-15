"""add unified inbox

Revision ID: a82f4c1d9e30
Revises: 9c31e2d4a7b0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a82f4c1d9e30"
down_revision = "9c31e2d4a7b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("handling_mode", sa.String(), nullable=False, server_default="auto"),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "assigned_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("conversations", sa.Column("last_read_at", sa.DateTime()))
    op.add_column("conversations", sa.Column("first_customer_message_at", sa.DateTime()))
    op.add_column("conversations", sa.Column("first_response_at", sa.DateTime()))
    op.add_column("conversations", sa.Column("last_customer_message_at", sa.DateTime()))
    op.add_column("conversations", sa.Column("last_seller_message_at", sa.DateTime()))
    op.create_index("ix_conversations_assigned_user_id", "conversations", ["assigned_user_id"])

    op.create_table(
        "conversation_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_conversation_notes_conversation_id", "conversation_notes", ["conversation_id"]
    )

    op.create_table(
        "conversation_tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "name", name="uq_conversation_tags_name"),
    )
    op.create_index(
        "ix_conversation_tags_conversation_id", "conversation_tags", ["conversation_id"]
    )

    # Historical threads start read, but their timing fields remain useful for
    # baseline response reporting immediately after deployment.
    op.execute(sa.text("""
        UPDATE conversations AS conversation
        SET first_customer_message_at = timing.first_customer,
            last_customer_message_at = timing.last_customer,
            first_response_at = timing.first_response,
            last_seller_message_at = timing.last_seller
        FROM (
            SELECT conversation_id,
                   MIN(created_at) FILTER (WHERE sender_type = 'customer') AS first_customer,
                   MAX(created_at) FILTER (WHERE sender_type = 'customer') AS last_customer,
                   MIN(created_at) FILTER (WHERE sender_type IN ('assistant', 'seller')) AS first_response,
                   MAX(created_at) FILTER (WHERE sender_type = 'seller') AS last_seller
            FROM messages
            GROUP BY conversation_id
        ) AS timing
        WHERE conversation.id = timing.conversation_id
    """))


def downgrade() -> None:
    op.drop_table("conversation_tags")
    op.drop_table("conversation_notes")
    op.drop_index("ix_conversations_assigned_user_id", table_name="conversations")
    op.drop_column("conversations", "last_seller_message_at")
    op.drop_column("conversations", "last_customer_message_at")
    op.drop_column("conversations", "first_response_at")
    op.drop_column("conversations", "first_customer_message_at")
    op.drop_column("conversations", "last_read_at")
    op.drop_column("conversations", "unread_count")
    op.drop_column("conversations", "assigned_user_id")
    op.drop_column("conversations", "handling_mode")
