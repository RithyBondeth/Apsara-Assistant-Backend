"""add shop profile

Revision ID: b3e1f7a9c2d4
Revises: a82f4c1d9e30
"""

from alembic import op
import sqlalchemy as sa


revision = "b3e1f7a9c2d4"
down_revision = "a82f4c1d9e30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Free text on purpose. Delivery pricing in Cambodia is "Phnom Penh $1.5,
    # provinces $2.5 by J&T, free over $50" — a shape no fee table captures
    # without the seller learning the table first.
    op.add_column("users", sa.Column("shop_address", sa.Text()))
    op.add_column("users", sa.Column("shop_hours", sa.Text()))
    op.add_column("users", sa.Column("delivery_info", sa.Text()))
    op.add_column("users", sa.Column("shop_policies", sa.Text()))


def downgrade() -> None:
    op.drop_column("users", "shop_policies")
    op.drop_column("users", "delivery_info")
    op.drop_column("users", "shop_hours")
    op.drop_column("users", "shop_address")
