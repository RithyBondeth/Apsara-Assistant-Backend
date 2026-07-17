"""conversation takeover: ai_enabled + integration_id

Revision ID: e4f5a6b7c8d9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing conversations keep auto-replying, so the server default is true.
    op.add_column(
        'conversations',
        sa.Column('ai_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Nullable: rows created before this column (and dashboard-created ones)
    # have no known integration; senders fall back to the first active one.
    op.add_column(
        'conversations',
        sa.Column('integration_id', sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        'fk_conversations_integration_id',
        'conversations',
        'platform_integrations',
        ['integration_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_conversations_integration_id', 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'integration_id')
    op.drop_column('conversations', 'ai_enabled')
