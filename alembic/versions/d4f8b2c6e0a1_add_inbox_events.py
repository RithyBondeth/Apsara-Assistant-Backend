"""add inbox events

Revision ID: d4f8b2c6e0a1
Revises: c3e7a1b5d9f2
Create Date: 2026-10-23 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

from app.services.realtime import NOTIFY_FUNCTION, TRIGGERS, drop_sql, trigger_sql

revision: str = "d4f8b2c6e0a1"
down_revision: Union[str, None] = "c3e7a1b5d9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(NOTIFY_FUNCTION)
    for table in TRIGGERS:
        for statement in trigger_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in TRIGGERS:
        for statement in drop_sql(table):
            op.execute(statement)
    op.execute("DROP FUNCTION IF EXISTS notify_apsara_event()")
