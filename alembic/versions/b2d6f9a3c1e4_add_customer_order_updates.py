"""add customer order updates

Revision ID: b2d6f9a3c1e4
Revises: a1c5e8f2b9d3
Create Date: 2026-10-23 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d6f9a3c1e4"
down_revision: Union[str, None] = "a1c5e8f2b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("customer_updates_enabled", sa.Boolean(),
                                     nullable=False, server_default="true"))
    # Existing orders count as already announced, so turning this on does not
    # message every past customer about an order they finished with long ago.
    op.add_column("orders", sa.Column("customer_notified_events",
                                      postgresql.JSONB(astext_type=sa.Text()),
                                      nullable=False, server_default="[]"))
    op.execute("""
        UPDATE orders SET customer_notified_events = (
            SELECT coalesce(jsonb_agg(e), '[]'::jsonb) FROM unnest(
                ARRAY['confirmed', 'shipped', 'delivered', 'cancelled', 'paid']) AS e)
    """)


def downgrade() -> None:
    op.drop_column("orders", "customer_notified_events")
    op.drop_column("users", "customer_updates_enabled")
