import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.crypto import encrypt
from app.database import get_db
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.schemas.integration import (
    ConnectionCheck,
    IntegrationCreate,
    IntegrationOut,
    IntegrationUpdate,
)
from app.services.platforms import (
    STRIPE,
    TELEGRAM,
    check_credentials,
    register_telegram_webhook,
)

router = APIRouter()


def _webhook_secret_for(payload: IntegrationCreate) -> str | None:
    """The secret that will authenticate this connection's webhooks.

    Three different arrangements: Telegram takes one we generate and register
    with setWebhook; Stripe issues its own when the seller creates the endpoint,
    and it is stored encrypted because it is theirs; Messenger authenticates
    with an app-level signature and needs nothing here.
    """
    if payload.platform == TELEGRAM:
        return secrets.token_urlsafe(32)
    if payload.platform == STRIPE:
        return encrypt(payload.webhook_secret)
    return None


def _webhook_url(connection: PlatformConnection) -> str:
    base = f"{settings.API_BASE_URL.rstrip('/')}/api/v1/webhooks"
    if connection.platform == TELEGRAM:
        return f"{base}/telegram/{connection.id}"
    if connection.platform == STRIPE:
        # Per connection, like Telegram: a Stripe event names the account it
        # came from only in fields the signature covers, and the signature can
        # only be checked once we know which seller's secret to check it with.
        return f"{base}/stripe/{connection.id}"
    return f"{base}/messenger"


def _to_out(connection: PlatformConnection) -> IntegrationOut:
    url = _webhook_url(connection)
    return IntegrationOut(
        id=connection.id,
        platform=connection.platform,
        external_id=connection.external_id,
        display_name=connection.display_name,
        is_active=connection.is_active,
        auto_reply=connection.auto_reply,
        created_at=connection.created_at,
        webhook_url=url,
        # Telegram's secret is generated here, so the seller has to be able to
        # read it back. Stripe's belongs to the seller and is stored encrypted;
        # handing it back out would be a read path onto someone else's secret.
        webhook_secret=connection.webhook_secret if connection.platform == TELEGRAM else None,
    )


@router.get("/", response_model=list[IntegrationOut])
def list_integrations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connections = (
        db.query(PlatformConnection)
        .filter(PlatformConnection.user_id == current_user.id)
        .order_by(PlatformConnection.created_at.desc())
        .all()
    )
    return [_to_out(c) for c in connections]


@router.post("/", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
def create_integration(
    payload: IntegrationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.platform == STRIPE and not payload.webhook_secret:
        # Without it there is no way to prove a payment notification is Stripe's,
        # and an unauthenticated one would let anyone mark orders paid. Refused
        # at connect time rather than discovered on the first real payment.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe needs the signing secret (whsec_...) from the webhook "
                   "endpoint you added in the Stripe dashboard.",
        )

    connection = PlatformConnection(
        user_id=current_user.id,
        platform=payload.platform,
        external_id=payload.external_id,
        display_name=payload.display_name,
        access_token=encrypt(payload.access_token),
        webhook_secret=_webhook_secret_for(payload),
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # The uniqueness of (platform, external_id) is what stops one seller
        # claiming a page another has already connected and receiving their
        # customers' messages.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That page or bot is already connected.",
        )
    db.refresh(connection)
    return _to_out(connection)


@router.post("/{integration_id}/check", response_model=ConnectionCheck)
def check_integration(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ask the platform whether this connection's token still works.

    Silence on a webhook looks identical whether the page is quiet or the
    token has been revoked, so this asks directly.
    """
    connection = _owned(db, integration_id, current_user)
    result = check_credentials(connection.platform, connection.access_token)
    return ConnectionCheck(ok=result.ok, detail=result.detail)


@router.post("/{integration_id}/register-webhook", response_model=ConnectionCheck)
def register_webhook(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Point a Telegram bot at our webhook.

    Messenger has no equivalent: its webhook is configured once per Meta app in
    the dashboard, not per connected page, so there is nothing to call here.
    """
    connection = _owned(db, integration_id, current_user)
    if connection.platform != TELEGRAM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messenger webhooks are configured once in the Meta app "
                   "dashboard, not per page.",
        )

    result = register_telegram_webhook(
        connection.access_token, _webhook_url(connection), connection.webhook_secret
    )
    return ConnectionCheck(ok=result.ok, detail=result.detail)


@router.patch("/{integration_id}", response_model=IntegrationOut)
def update_integration(
    integration_id: UUID,
    payload: IntegrationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connection = _owned(db, integration_id, current_user)

    data = payload.model_dump(exclude_unset=True)
    if "access_token" in data:
        data["access_token"] = encrypt(data["access_token"])
    for field, value in data.items():
        setattr(connection, field, value)

    db.commit()
    db.refresh(connection)
    return _to_out(connection)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.delete(_owned(db, integration_id, current_user))
    db.commit()


def _owned(db: Session, integration_id: UUID, user: User) -> PlatformConnection:
    connection = (
        db.query(PlatformConnection)
        .filter(PlatformConnection.id == integration_id,
                PlatformConnection.user_id == user.id)
        .first()
    )
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Integration not found")
    return connection
