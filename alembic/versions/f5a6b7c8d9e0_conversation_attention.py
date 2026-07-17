"""conversation attention: needs_attention, last_seen_at, last_customer_message_at

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'conversations',
        sa.Column('needs_attention', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('conversations', sa.Column('last_seen_at', sa.DateTime(), nullable=True))
    op.add_column(
        'conversations', sa.Column('last_customer_message_at', sa.DateTime(), nullable=True)
    )

    # Backfill from the messages already on record, so existing threads show a
    # truthful unread state instead of all reading as "nothing new".
    op.execute(
        """
        UPDATE conversations c
        SET last_customer_message_at = m.last_at
        FROM (
            SELECT conversation_id, MAX(created_at) AS last_at
            FROM messages
            WHERE sender_type = 'customer'
            GROUP BY conversation_id
        ) m
        WHERE m.conversation_id = c.id
        """
    )

    # The inbox's default view is "what needs me", so it filters on these.
    op.create_index(
        'ix_conversations_user_attention', 'conversations', ['user_id', 'needs_attention']
    )


def downgrade() -> None:
    op.drop_index('ix_conversations_user_attention', table_name='conversations')
    op.drop_column('conversations', 'last_customer_message_at')
    op.drop_column('conversations', 'last_seen_at')
    op.drop_column('conversations', 'needs_attention')
