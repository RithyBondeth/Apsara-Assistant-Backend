"""distinguish channel threads from rehearsals

Revision ID: 8b5e9f3a2c71
Revises: 7a4d8e2f1b60
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b5e9f3a2c71"
down_revision: Union[str, None] = "7a4d8e2f1b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("source", sa.String(), server_default="rehearsal", nullable=False),
    )
    # Best-effort classification for threads created before connection ids were
    # stored. A platform customer id means the thread originated externally.
    op.execute("""
        UPDATE conversations AS c
        SET source = 'channel'
        FROM customers AS customer
        WHERE customer.id = c.customer_id
          AND customer.platform_id IS NOT NULL
          AND c.platform IN ('messenger', 'telegram')
    """)


def downgrade() -> None:
    op.drop_column("conversations", "source")
