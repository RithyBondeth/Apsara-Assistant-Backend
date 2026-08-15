"""add ai draft quota

Revision ID: a4d7c2e91f30
Revises: 9c6e4a1d7b82
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4d7c2e91f30"
down_revision: Union[str, None] = "9c6e4a1d7b82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_usage",
        sa.Column("draft_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_usage", "draft_count")
