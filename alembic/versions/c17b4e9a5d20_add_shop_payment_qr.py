"""add shop payment qr

Revision ID: c17b4e9a5d20
Revises: 3d35e4d19d7f
Create Date: 2026-08-10 03:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c17b4e9a5d20'
down_revision: Union[str, None] = '3d35e4d19d7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('payment_qr_url', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'payment_qr_url')
