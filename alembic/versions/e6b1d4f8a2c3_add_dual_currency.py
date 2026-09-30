"""add dual currency

Revision ID: e6b1d4f8a2c3
Revises: d5a9c3e7b1f2
"""

from alembic import op
import sqlalchemy as sa


revision = "e6b1d4f8a2c3"
down_revision = "d5a9c3e7b1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cambodia is bimonetary: shops price in dollars and customers pay in
    # riel at the shop's own rate. The rate lets the assistant quote both
    # and lets a riel receipt be checked against a dollar order.
    op.add_column(
        "users",
        sa.Column("khr_rate", sa.Numeric(10, 2), nullable=False, server_default="4100"),
    )
    # What was actually paid, when it differs from what the order was priced
    # in — 32,000 riel against an 8-dollar order is a fact worth keeping.
    op.add_column("orders", sa.Column("paid_amount", sa.Numeric(12, 2)))
    op.add_column("orders", sa.Column("paid_currency", sa.String(length=3)))


def downgrade() -> None:
    op.drop_column("orders", "paid_currency")
    op.drop_column("orders", "paid_amount")
    op.drop_column("users", "khr_rate")
