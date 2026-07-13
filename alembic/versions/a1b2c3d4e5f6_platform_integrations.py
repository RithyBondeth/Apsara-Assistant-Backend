"""platform integrations

Revision ID: a1b2c3d4e5f6
Revises: d98a957b8869
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'd98a957b8869'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'platform_integrations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('access_token', sa.String(), nullable=False),
        sa.Column('secret_token', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'external_id', name='uq_platform_external_id'),
    )
    op.create_index(
        op.f('ix_platform_integrations_external_id'),
        'platform_integrations',
        ['external_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_platform_integrations_external_id'),
        table_name='platform_integrations',
    )
    op.drop_table('platform_integrations')
