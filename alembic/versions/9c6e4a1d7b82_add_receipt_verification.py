"""add receipt verification

Revision ID: 9c6e4a1d7b82
Revises: 8b5e9f3a2c71
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9c6e4a1d7b82"
down_revision: Union[str, None] = "8b5e9f3a2c71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("attachments", "file_url", existing_type=sa.String(), nullable=True)
    op.add_column("attachments", sa.Column("blob", sa.LargeBinary(), nullable=True))
    op.add_column("attachments", sa.Column("review_status", sa.String(), nullable=True))
    op.add_column("attachments", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("attachments", sa.Column(
        "reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
    ))
    op.create_foreign_key(
        "fk_attachments_reviewed_by_user_id_users", "attachments", "users",
        ["reviewed_by_user_id"], ["id"], ondelete="SET NULL",
    )

    op.add_column("orders", sa.Column("payment_method", sa.String(), nullable=True))
    op.add_column("orders", sa.Column(
        "payment_receipt_attachment_id", postgresql.UUID(as_uuid=True), nullable=True
    ))
    op.add_column("orders", sa.Column(
        "payment_confirmed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
    ))
    op.create_foreign_key(
        "fk_orders_payment_receipt_attachment_id_attachments", "orders", "attachments",
        ["payment_receipt_attachment_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_orders_payment_confirmed_by_user_id_users", "orders", "users",
        ["payment_confirmed_by_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_orders_payment_receipt_attachment_id", "orders",
        ["payment_receipt_attachment_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_orders_payment_receipt_attachment_id", "orders", type_="unique")
    op.drop_constraint("fk_orders_payment_confirmed_by_user_id_users", "orders", type_="foreignkey")
    op.drop_constraint("fk_orders_payment_receipt_attachment_id_attachments", "orders", type_="foreignkey")
    op.drop_column("orders", "payment_confirmed_by_user_id")
    op.drop_column("orders", "payment_receipt_attachment_id")
    op.drop_column("orders", "payment_method")

    op.drop_constraint("fk_attachments_reviewed_by_user_id_users", "attachments", type_="foreignkey")
    op.drop_column("attachments", "reviewed_by_user_id")
    op.drop_column("attachments", "reviewed_at")
    op.drop_column("attachments", "review_status")
    op.drop_column("attachments", "blob")
    op.alter_column("attachments", "file_url", existing_type=sa.String(), nullable=False)
