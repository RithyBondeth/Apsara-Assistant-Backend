"""add email verification, sign-up throttling and data deletion requests

Revision ID: f6b0d4e8a2c5
Revises: e5a9c3d7f1b4
Create Date: 2026-10-23 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6b0d4e8a2c5"
down_revision: Union[str, None] = "e5a9c3d7f1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    # Accounts from before verification existed keep signing in: they are
    # treated as confirmed from the day they were made.
    op.execute("UPDATE users SET email_verified_at = coalesce(created_at, now() AT TIME ZONE 'utc')")

    op.create_table(
        "signup_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_signup_attempts_created_at", "signup_attempts", ["created_at"])
    op.create_index("ix_signup_attempts_ip_created_at", "signup_attempts", ["ip", "created_at"])

    op.create_table(
        "data_deletion_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("confirmation_code", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("customers_erased", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("confirmation_code"),
    )


def downgrade() -> None:
    op.drop_table("data_deletion_requests")
    op.drop_index("ix_signup_attempts_ip_created_at", table_name="signup_attempts")
    op.drop_index("ix_signup_attempts_created_at", table_name="signup_attempts")
    op.drop_table("signup_attempts")
    op.drop_column("users", "email_verified_at")
