"""add object storage for media

Revision ID: a1c5e8f2b9d3
Revises: e6b1d4f8a2c3
Create Date: 2026-10-23 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.models.storage_tombstone import MEDIA_TABLES, TOMBSTONE_FUNCTION, tombstone_trigger

revision: str = "a1c5e8f2b9d3"
down_revision: Union[str, None] = "e6b1d4f8a2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "storage_tombstones",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_storage_tombstones_created_at", "storage_tombstones", ["created_at"])

    for table in MEDIA_TABLES:
        op.add_column(table, sa.Column("storage_key", sa.String(length=512), nullable=True))
    # The bytes may now live in the bucket instead.
    op.alter_column("product_images", "blob", existing_type=sa.LargeBinary(), nullable=True)
    op.alter_column("payment_qrs", "blob", existing_type=sa.LargeBinary(), nullable=True)

    op.execute(TOMBSTONE_FUNCTION)
    for table in MEDIA_TABLES:
        op.execute(tombstone_trigger(table))


def downgrade() -> None:
    for table in MEDIA_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_storage_tombstone ON {table}")
    op.execute("DROP FUNCTION IF EXISTS record_storage_tombstone()")
    # Rows whose bytes are only in the bucket cannot satisfy NOT NULL again;
    # move them back first (python -m app.scripts.move_media --to database).
    op.alter_column("payment_qrs", "blob", existing_type=sa.LargeBinary(), nullable=False)
    op.alter_column("product_images", "blob", existing_type=sa.LargeBinary(), nullable=False)
    for table in MEDIA_TABLES:
        op.drop_column(table, "storage_key")
    op.drop_index("ix_storage_tombstones_created_at", table_name="storage_tombstones")
    op.drop_table("storage_tombstones")
