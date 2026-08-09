"""How many assistant replies a seller may spend in a day.

Inbound volume stopped being ours to control the moment webhooks landed: a busy
page, or someone deliberately messaging a bot in a loop, turns straight into
OpenAI spend. This is the ceiling on that.
"""

import logging
from datetime import date

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_usage import AiUsage

logger = logging.getLogger(__name__)


def spend_reply(db: Session, user_id) -> bool:
    """Count one reply against today's allowance.

    Returns whether it was within budget. Incremented atomically in a single
    upsert, so two messages arriving together cannot both read the same count
    and each decide they were the last one under the limit.
    """
    if settings.AI_DAILY_REPLY_LIMIT <= 0:
        return True

    statement = (
        insert(AiUsage)
        .values(user_id=user_id, day=date.today(), count=1)
        .on_conflict_do_update(
            constraint="uq_ai_usage_user_day",
            set_={"count": AiUsage.count + 1},
        )
        .returning(AiUsage.count)
    )
    used = db.execute(statement).scalar_one()
    db.commit()

    if used > settings.AI_DAILY_REPLY_LIMIT:
        logger.warning("Seller %s is over the daily reply limit (%s)",
                       user_id, settings.AI_DAILY_REPLY_LIMIT)
        return False
    return True


def used_today(db: Session, user_id) -> int:
    row = (
        db.query(AiUsage.count)
        .filter(AiUsage.user_id == user_id, AiUsage.day == date.today())
        .first()
    )
    return row[0] if row else 0
