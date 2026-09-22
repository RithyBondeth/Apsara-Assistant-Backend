"""add receipt ocr

Revision ID: d5a9c3e7b1f2
Revises: c4f8a2d6e1b7
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d5a9c3e7b1f2"
down_revision = "c4f8a2d6e1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # What the model read off a customer's receipt. Amount, currency and
    # reference are columns because they are compared and searched — the
    # reference across every receipt the seller has ever been sent, to catch
    # the same transfer offered twice. The rest is kept whole in ocr_data.
    op.add_column("attachments", sa.Column("ocr_status", sa.String(length=16)))
    op.add_column("attachments", sa.Column("ocr_amount", sa.Numeric(12, 2)))
    op.add_column("attachments", sa.Column("ocr_currency", sa.String(length=3)))
    op.add_column("attachments", sa.Column("ocr_reference", sa.String(length=100)))
    op.add_column("attachments", sa.Column("ocr_data", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column("attachments", sa.Column("ocr_at", sa.DateTime()))
    op.create_index("ix_attachments_ocr_reference", "attachments", ["ocr_reference"])

    op.add_column(
        "ai_usage",
        sa.Column("receipt_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ai_usage", "receipt_count")
    op.drop_index("ix_attachments_ocr_reference", table_name="attachments")
    op.drop_column("attachments", "ocr_at")
    op.drop_column("attachments", "ocr_data")
    op.drop_column("attachments", "ocr_reference")
    op.drop_column("attachments", "ocr_currency")
    op.drop_column("attachments", "ocr_amount")
    op.drop_column("attachments", "ocr_status")
