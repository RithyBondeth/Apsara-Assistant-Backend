"""add staff accounts

Revision ID: e5a9c3d7f1b4
Revises: d4f8b2c6e0a1
Create Date: 2026-10-23 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a9c3d7f1b4"
down_revision: Union[str, None] = "d4f8b2c6e0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("shop_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("users_shop_id_fkey", "users", "users", ["shop_id"], ["id"],
                          ondelete="CASCADE")
    op.create_index("ix_users_shop_id", "users", ["shop_id"])
    op.add_column("users", sa.Column("role", sa.String(length=16), nullable=False,
                                     server_default="owner"))


def downgrade() -> None:
    op.drop_column("users", "role")
    op.drop_index("ix_users_shop_id", table_name="users")
    op.drop_constraint("users_shop_id_fkey", "users", type_="foreignkey")
    op.drop_column("users", "shop_id")
