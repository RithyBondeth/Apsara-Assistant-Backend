"""add login attempts

Revision ID: c0a367fcf249
Revises: c17b4e9a5d20
Create Date: 2026-08-10 15:02:21.019569

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c0a367fcf249'
down_revision: Union[str, None] = 'c17b4e9a5d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'login_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Keyed lookups are always "this email/address, within the window", so the
    # timestamp belongs in the index beside the key rather than in a filter.
    op.create_index('ix_login_attempts_email_created_at', 'login_attempts',
                    ['email', 'created_at'], unique=False)
    op.create_index('ix_login_attempts_ip_created_at', 'login_attempts',
                    ['ip', 'created_at'], unique=False)
    # Age alone, for the sweep that drops rows too old to count.
    op.create_index(op.f('ix_login_attempts_created_at'), 'login_attempts',
                    ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_login_attempts_created_at'), table_name='login_attempts')
    op.drop_index('ix_login_attempts_ip_created_at', table_name='login_attempts')
    op.drop_index('ix_login_attempts_email_created_at', table_name='login_attempts')
    op.drop_table('login_attempts')
