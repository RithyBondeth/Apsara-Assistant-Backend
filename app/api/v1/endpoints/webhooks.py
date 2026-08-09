"""Inbound webhooks from Facebook Messenger and Telegram.

These are the only unauthenticated endpoints in the API, so each one proves the
caller is really the platform before it does anything: Messenger by an app-level
signature over the raw body, Telegram by a per-connection secret header.

They also answer 200 for anything they cannot use. A webhook that returns an
error is retried — often for hours — so reporting "this payload was not for me"
as a failure produces a retry storm rather than a fix.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.platform_connection import PlatformConnection
from app.services import platforms
from app.services.inbound import handle_inbound

logger = logging.getLogger(__name__)

router = APIRouter()

ACK = {"status": "received"}


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
            background.add_task(handle_inbound, connection.id, message)

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
        background.add_task(handle_inbound, connection.id, message)

    return ACK


def _connection_by_external_id(db: Session, platform: str, external_id: str):
    return (
        db.query(PlatformConnection)
        .filter(PlatformConnection.platform == platform,
                PlatformConnection.external_id == str(external_id),
                PlatformConnection.is_active == True)
        .first()
    )
