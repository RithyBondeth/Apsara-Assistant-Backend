"""Inbound webhooks from Facebook Messenger and Telegram.

These are the only unauthenticated endpoints in the API, so each one proves the
caller is really the platform before it does anything: Messenger by an app-level
signature over the raw body, Telegram by a per-connection secret header.

They also answer 200 for anything they cannot use. A webhook that returns an
error is retried — often for hours — so reporting "this payload was not for me"
as a failure produces a retry storm rather than a fix.
"""

import dataclasses
import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.database import get_db
from app.models.order import PAID, Order
from app.models.platform_connection import PlatformConnection
from app.services import platforms, stripe_gateway
from app.services.inbound import INBOUND_MESSAGE
from app.services.queue import drain, enqueue

logger = logging.getLogger(__name__)

router = APIRouter()

ACK = {"status": "received"}


def _queue(db: Session, background: BackgroundTasks, connection_id, message) -> None:
    """Persist the work, then nudge it along.

    The row is what makes this durable; the background nudge is only so a
    single-process deployment needs no separate worker. If the process dies
    before or during the nudge, the job is still on the queue for a worker — or
    the next restart — to pick up.
    """
    enqueue(db, INBOUND_MESSAGE, {
        "connection_id": str(connection_id),
        "message": dataclasses.asdict(message),
    })
    db.commit()
    if settings.JOB_RUNNER == "inline":
        background.add_task(drain, 5)


# ── Facebook Messenger ───────────────────────────────────────────────────────

@router.get("/messenger")
def verify_messenger_subscription(request: Request):
    """Answer Meta's subscription handshake by echoing its challenge."""
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and settings.META_VERIFY_TOKEN
            and params.get("hub.verify_token") == settings.META_VERIFY_TOKEN):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/messenger")
async def receive_messenger_event(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
):
    # Read the body as bytes and sign those exact bytes. Parsing first and
    # re-serialising would change spacing or key order and break the digest.
    raw = await request.body()
    if not platforms.verify_messenger_signature(raw, x_hub_signature_256):
        # One of the few places a 4xx is right: an unsigned caller is not Meta,
        # and there is no delivery worth retrying.
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    if payload.get("object") != "page":
        return ACK

    for entry in payload.get("entry") or []:
        page_id, messages = platforms.parse_messenger_entry(entry)
        if not (page_id and messages):
            continue

        connection = _connection_by_external_id(db, platforms.MESSENGER, page_id)
        if not connection:
            # A page nobody has connected. Acknowledged so Meta stops resending.
            logger.warning("Messenger event for unconnected page %s", page_id)
            continue

        for message in messages:
            _queue(db, background, connection.id, message)

    return ACK


# ── Telegram ─────────────────────────────────────────────────────────────────

@router.post("/telegram/{connection_id}")
async def receive_telegram_update(
    connection_id: UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Receive one update for a specific bot.

    Telegram puts nothing in the payload identifying which bot was messaged, so
    the connection is named in the path and authenticated by the secret header
    that was registered with setWebhook.
    """
    connection = (
        db.query(PlatformConnection)
        .filter(PlatformConnection.id == connection_id,
                PlatformConnection.platform == platforms.TELEGRAM,
                PlatformConnection.is_active == True)
        .first()
    )
    # Same answer whether the connection is unknown or the secret is wrong, so
    # the endpoint cannot be used to discover which connection ids exist.
    if not connection or not platforms.verify_telegram_secret(
        connection.webhook_secret, x_telegram_bot_api_secret_token
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook credentials")

    message = platforms.parse_telegram_update(await request.json())
    if message:
        _queue(db, background, connection.id, message)

    return ACK


# ── Stripe ───────────────────────────────────────────────────────────────────

@router.post("/stripe/{connection_id}")
async def receive_stripe_event(
    connection_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    stripe_signature: str | None = Header(default=None),
):
    """Confirm a payment.

    This is the only thing that may mark an order paid. The customer's browser
    returning to a success URL proves nothing — it can be visited directly, and
    it does not happen at all if they close the tab — so the money is only
    believed when Stripe says so over a signed channel.

    Handled inline rather than queued: it is a signature check and one update,
    and a seller watching an order flip to paid should not wait on a poll.
    """
    connection = (
        db.query(PlatformConnection)
        .filter(PlatformConnection.id == connection_id,
                PlatformConnection.platform == platforms.STRIPE,
                PlatformConnection.is_active == True)
        .first()
    )
    # Signed over the exact bytes, so read the body before anything parses it.
    raw = await request.body()
    if not connection:
        raise HTTPException(status_code=403, detail="Invalid webhook credentials")

    event = stripe_gateway.verify_webhook(raw, stripe_signature, connection.webhook_secret)
    if event is None:
        raise HTTPException(status_code=403, detail="Invalid webhook credentials")

    if event.get("type") != "checkout.session.completed":
        # Stripe sends whatever the endpoint subscribed to. Anything else is
        # acknowledged so it is not retried for hours.
        return ACK

    session = (event.get("data") or {}).get("object") or {}
    order_id = (session.get("metadata") or {}).get("order_id")
    if not order_id:
        logger.warning("Stripe session %s carried no order id", session.get("id"))
        return ACK

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.user_id == connection.user_id)
        .first()
    )
    if not order:
        # Scoped to the connection's owner, so one seller's Stripe account
        # cannot mark another seller's order paid by quoting its id.
        logger.warning("Stripe event for unknown order %s", order_id)
        return ACK

    if order.payment_status != PAID:
        # Stripe redelivers on any doubt about receipt, so this runs more than
        # once for a single payment. Writing only on the transition keeps
        # paid_at the time of the payment rather than the last retry.
        order.payment_status = PAID
        order.payment_method = "stripe"
        order.paid_at = utcnow()
        # Paid stock must never be released by reservation expiry even if the
        # seller has not yet moved the order from pending to confirmed.
        order.reservation_expires_at = None
        db.commit()
        logger.info("Order %s marked paid from Stripe session %s",
                    order.id, session.get("id"))

    return ACK


def _connection_by_external_id(db: Session, platform: str, external_id: str):
    return (
        db.query(PlatformConnection)
        .filter(PlatformConnection.platform == platform,
                PlatformConnection.external_id == str(external_id),
                PlatformConnection.is_active == True)
        .first()
    )
