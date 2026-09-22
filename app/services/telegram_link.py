"""Linking a seller's own Telegram to their bot — without a chat id in sight.

Every Telegram alert goes to the seller's private chat with the bot they
connected, and Telegram identifies that chat by a number the seller has no
way of knowing. Asking them to find it was the wrong shape for this product.
Instead, Settings shows a link; opening it starts a chat with the bot and
sends `/start <code>`; the webhook recognises the code and records the chat
it came from. The seller taps once, and never sees a number.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.models.verification_code import VerificationCode
from app.services.platforms import TELEGRAM, InboundMessage, send_reply, telegram_bot_username
from app.services.seller_alerts import render
from app.services.verification import hash_code, invalidate_outstanding

logger = logging.getLogger(__name__)

PURPOSE = "telegram_link"

# Long enough to walk over to the phone; short enough that a link pasted
# somewhere by mistake stops working before it matters.
EXPIRE_MINUTES = 15

# What Telegram delivers when a t.me/<bot>?start=<code> link is opened. The
# code alphabet is what deep links allow; anything else is a customer who
# happened to type "/start".
START_WITH_CODE = re.compile(r"^/start\s+([A-Za-z0-9_-]{8,64})$")

EXPIRED = {
    "en": "This link has expired. Open Settings in Apsara and tap Link Telegram again.",
    "km": "តំណនេះផុតកំណត់ហើយ។ សូមបើកការកំណត់ក្នុង Apsara ហើយចុច ភ្ជាប់ Telegram ម្តងទៀត។",
}


def issue(db: Session, user: User, connection: PlatformConnection) -> tuple[str, str]:
    """A fresh link for `user`, through `connection`'s bot: (code, url).

    Issuing a new one retires the previous, so only the link on screen works.
    """
    code = secrets.token_urlsafe(12)
    invalidate_outstanding(db, user, PURPOSE)
    db.add(VerificationCode(
        user_id=user.id,
        purpose=PURPOSE,
        code_hash=hash_code(code),
        expires_at=utcnow() + timedelta(minutes=EXPIRE_MINUTES),
    ))
    db.commit()
    username = telegram_bot_username(connection.access_token) \
        or connection.external_id.lstrip("@")
    return code, f"https://t.me/{username}?start={code}"


def claim(db: Session, connection: PlatformConnection, message: InboundMessage) -> bool:
    """Handle a `/start <code>` if that is what this is.

    Returns True when the message was a link attempt — valid or not — and so
    must not go on to become a customer conversation. A seller who opens a
    stale link gets told so by the bot, rather than being answered by their
    own assistant as if they were a customer.
    """
    match = START_WITH_CODE.match((message.text or "").strip())
    if not match:
        return False
    code = match.group(1)

    now = utcnow()
    record = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.purpose == PURPOSE,
            # Scoped to the bot's owner: a code is only good with the seller's
            # own bot, so one seller's link opened against another's bot does
            # nothing.
            VerificationCode.user_id == connection.user_id,
            VerificationCode.consumed_at.is_(None),
            VerificationCode.expires_at > now,
        )
        .order_by(VerificationCode.created_at.desc())
        .first()
    )
    user = db.query(User).filter(User.id == connection.user_id).first()
    language = user.language if user else "en"

    if record is None or not hmac.compare_digest(record.code_hash, hash_code(code)):
        send_reply(TELEGRAM, connection.access_token, message.sender_id,
                   EXPIRED.get(language) or EXPIRED["en"])
        return True

    record.consumed_at = now
    user.telegram_chat_id = message.sender_id
    user.telegram_chat_name = message.sender_name
    user.telegram_linked_at = now
    db.commit()
    logger.info("Seller %s linked Telegram chat %s", user.id, message.sender_id)

    send_reply(TELEGRAM, connection.access_token, message.sender_id,
               render("telegram_linked", language,
                      shop=user.business_name or user.full_name))
    return True
