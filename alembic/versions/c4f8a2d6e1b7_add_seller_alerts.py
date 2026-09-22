"""add seller alerts

Revision ID: c4f8a2d6e1b7
Revises: b3e1f7a9c2d4
"""

from alembic import op
import sqlalchemy as sa


revision = "c4f8a2d6e1b7"
down_revision = "b3e1f7a9c2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The chat id was only ever the seller's own chat with their bot; it now
    # carries every kind of alert, so its name stops claiming otherwise. The
    # value is kept — sellers who typed one in stay linked.
    op.alter_column("users", "low_stock_telegram_chat_id", new_column_name="telegram_chat_id")
    op.add_column("users", sa.Column("telegram_chat_name", sa.String(length=200)))
    op.add_column("users", sa.Column("telegram_linked_at", sa.DateTime()))
    op.add_column(
        "users",
        sa.Column("attention_telegram_enabled", sa.Boolean(), nullable=False,
                  server_default="true"),
    )
    op.add_column(
        "users",
        sa.Column("payment_telegram_enabled", sa.Boolean(), nullable=False,
                  server_default="true"),
    )
    # Alerts are written in the seller's language; the web app keeps this in
    # step with the language the seller picked there.
    op.add_column(
        "users",
        sa.Column("language", sa.String(length=2), nullable=False, server_default="en"),
    )

    op.add_column("conversations", sa.Column("needs_attention_at", sa.DateTime()))
    op.create_index(
        "ix_conversations_needs_attention_at", "conversations", ["needs_attention_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_needs_attention_at", table_name="conversations")
    op.drop_column("conversations", "needs_attention_at")
    op.drop_column("users", "language")
    op.drop_column("users", "payment_telegram_enabled")
    op.drop_column("users", "attention_telegram_enabled")
    op.drop_column("users", "telegram_linked_at")
    op.drop_column("users", "telegram_chat_name")
    op.alter_column("users", "telegram_chat_id", new_column_name="low_stock_telegram_chat_id")
