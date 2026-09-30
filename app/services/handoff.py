"""When the assistant hands a thread to the seller, and when it takes it back.

A seller who answers a customer themselves is saying "I have got this one",
not "never speak on this thread again". Before this, a reply silenced the
assistant on that thread forever: a shop that answered ten customers on
Monday had ten threads the assistant would not touch on Tuesday, with no
sign of why. Pressing *Take over* is the other case — that one is a standing
instruction and is left alone until the seller hands the thread back.

So the two are recorded separately, and only a reply expires.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.core.clock import utcnow
from app.models.conversation import Conversation
from app.models.user import User

logger = logging.getLogger(__name__)

AUTO = "auto"
MANUAL = "manual"

# Why a thread is manual.
REPLY = "reply"        # the seller answered it; expires after their timeout
EXPLICIT = "explicit"  # the seller pressed Take over; stays until returned


def take_over(conversation: Conversation, source: str, user_id=None) -> None:
    """Put a thread in the seller's hands, recording why."""
    conversation.handling_mode = MANUAL
    conversation.manual_mode_source = source
    if user_id is not None:
        conversation.assigned_user_id = user_id


def hand_back(conversation: Conversation) -> None:
    """Return a thread to the assistant."""
    conversation.handling_mode = AUTO
    conversation.manual_mode_source = None
    conversation.assigned_user_id = None


def resume_due(conversation: Conversation, seller: User) -> bool:
    """Whether the assistant should pick this thread back up now.

    Only threads the seller merely replied to, only once their timeout has
    passed with no further word from them, and never when they have turned
    the timeout off.
    """
    if conversation.handling_mode != MANUAL:
        return False
    if conversation.manual_mode_source != REPLY:
        return False
    timeout = seller.manual_timeout_hours or 0
    if timeout <= 0:
        return False
    # A thread marked as a reply always has a reply behind it; if that is
    # somehow missing there is no clock to run, so leave it with the seller.
    since = conversation.last_seller_message_at
    if since is None:
        return False
    return utcnow() - since >= timedelta(hours=timeout)


def resume_if_due(conversation: Conversation, seller: User) -> bool:
    """Hand the thread back to the assistant if its quiet period has passed.

    Returns whether it did. The caller commits — this runs inside the inbound
    path, where the customer's message is being stored in the same
    transaction.
    """
    if not resume_due(conversation, seller):
        return False
    hand_back(conversation)
    logger.info("Assistant resumed conversation %s after %sh of seller silence",
                conversation.id, seller.manual_timeout_hours)
    return True
