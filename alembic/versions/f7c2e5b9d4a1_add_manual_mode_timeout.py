"""add manual mode timeout

Revision ID: f7c2e5b9d4a1
Revises: e6b1d4f8a2c3
"""

from alembic import op
import sqlalchemy as sa


revision = "f7c2e5b9d4a1"
down_revision = "e6b1d4f8a2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Hours of seller silence after which the assistant picks a thread back
    # up. 0 means never, which is how it behaved before this.
    op.add_column(
        "users",
        sa.Column("manual_timeout_hours", sa.Integer(), nullable=False, server_default="12"),
    )
    # Why this thread is manual: the seller replied ("reply"), or they
    # deliberately took it over ("explicit"). Only a reply expires.
    op.add_column("conversations", sa.Column("manual_mode_source", sa.String(length=10)))

    # Existing manual threads have no recorded reason. A thread carrying a
    # seller message was, at minimum, made manual by that reply, so it is
    # treated as one and can expire; a manual thread with no seller message
    # can only have come from the button, and is left alone. A seller who
    # both replied and pressed the button is read as having replied — the
    # side that resumes, which is the point of the feature.
    op.execute(sa.text("""
        UPDATE conversations
        SET manual_mode_source = CASE
            WHEN last_seller_message_at IS NOT NULL THEN 'reply'
            ELSE 'explicit'
        END
        WHERE handling_mode = 'manual'
    """))


def downgrade() -> None:
    op.drop_column("conversations", "manual_mode_source")
    op.drop_column("users", "manual_timeout_hours")
