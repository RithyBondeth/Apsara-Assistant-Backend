"""add dynamic KHQR

Revision ID: c3e7a1b5d9f2
Revises: b2d6f9a3c1e4
Create Date: 2026-10-23 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e7a1b5d9f2"
down_revision: Union[str, None] = "b2d6f9a3c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("khqr_enabled", sa.Boolean(), nullable=False,
                                     server_default="false"))
    op.add_column("users", sa.Column("bakong_account_id", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("khqr_merchant_name", sa.String(length=25), nullable=True))
    op.add_column("users", sa.Column("khqr_merchant_city", sa.String(length=15), nullable=True))
    op.add_column("users", sa.Column("khqr_merchant_id", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("khqr_acquiring_bank", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("bakong_api_token", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("khqr_payload", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("khqr_md5", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("khqr_expires_at", sa.DateTime(), nullable=True))
    op.add_column("orders", sa.Column("khqr_status", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("khqr_checked_at", sa.DateTime(), nullable=True))
    op.create_index("ix_orders_khqr_md5", "orders", ["khqr_md5"])


def downgrade() -> None:
    op.drop_index("ix_orders_khqr_md5", table_name="orders")
    for column in ("khqr_checked_at", "khqr_status", "khqr_expires_at", "khqr_md5", "khqr_payload"):
        op.drop_column("orders", column)
    for column in ("bakong_api_token", "khqr_acquiring_bank", "khqr_merchant_id",
                   "khqr_merchant_city", "khqr_merchant_name", "bakong_account_id",
                   "khqr_enabled"):
        op.drop_column("users", column)
