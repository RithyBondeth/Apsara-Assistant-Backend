from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import errors
from app.core.config import settings
from app.core.platforms import META_PLATFORMS, SUPPORTED_PLATFORMS, platform_list
from app.database import get_db
from app.models.platform_integration import PlatformIntegration
from app.models.user import User
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationOut,
    IntegrationUpdate,
    WebhookRegisterOut,
)
from app.services import messenger, telegram

router = APIRouter()


@router.get("/", response_model=list[IntegrationOut])
def list_integrations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PlatformIntegration)
        .filter(PlatformIntegration.user_id == current_user.id)
        .order_by(PlatformIntegration.created_at.desc())
        .all()
    )


@router.post("/", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
def create_integration(
    payload: IntegrationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.platform not in SUPPORTED_PLATFORMS:
        raise errors.unsupported_platform(platform_list())

    if payload.platform in META_PLATFORMS and not payload.app_secret:
        raise errors.app_secret_required(payload.platform.title())

    integration = PlatformIntegration(
        user_id=current_user.id,
        platform=payload.platform,
        access_token=payload.access_token,
        external_id=payload.external_id,
        secret_token=payload.secret_token,
        app_secret=payload.app_secret,
    )
    db.add(integration)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise errors.integration_already_connected()
    db.refresh(integration)
    return integration


def _get_owned_integration(db: Session, integration_id: UUID, user: User) -> PlatformIntegration:
    integration = db.query(PlatformIntegration).filter(
        PlatformIntegration.id == integration_id,
        PlatformIntegration.user_id == user.id,
    ).first()
    if not integration:
        raise errors.integration_not_found()
    return integration


@router.get("/{integration_id}", response_model=IntegrationOut)
def get_integration(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_owned_integration(db, integration_id, current_user)


@router.patch("/{integration_id}", response_model=IntegrationOut)
def update_integration(
    integration_id: UUID,
    payload: IntegrationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    integration = _get_owned_integration(db, integration_id, current_user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(integration, field, value)
    db.commit()
    db.refresh(integration)
    return integration


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    integration = _get_owned_integration(db, integration_id, current_user)
    db.delete(integration)
    db.commit()


@router.post("/{integration_id}/register-webhook", response_model=WebhookRegisterOut)
async def register_webhook(
    integration_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Point the platform's webhook at this API for the given integration."""
    integration = _get_owned_integration(db, integration_id, current_user)

    if not settings.PUBLIC_BASE_URL:
        raise errors.public_base_url_not_configured()

    base = settings.PUBLIC_BASE_URL.rstrip("/")
    webhook_url = f"{base}/api/v1/webhooks/{integration.platform}/{integration.id}"

    if integration.platform == "telegram":
        result = await telegram.set_webhook(
            integration.access_token, webhook_url, integration.secret_token
        )
        return WebhookRegisterOut(
            webhook_url=webhook_url,
            ok=bool(result.get("ok")),
            detail=result.get("description"),
        )

    if integration.platform == "messenger":
        # The callback URL + verify token are entered in the Facebook App
        # dashboard (pointing at webhook_url below). Here we subscribe the page
        # to the app so its message events start flowing.
        if not integration.external_id:
            raise errors.external_id_required()
        result = await messenger.subscribe_page(integration.access_token, integration.external_id)
        return WebhookRegisterOut(
            webhook_url=webhook_url,
            ok=bool(result.get("success")),
            detail="Set this URL + verify token in the Meta App dashboard, then subscribe the page.",
        )

    if integration.platform == "instagram":
        # Instagram (Instagram-login) webhooks are configured in the Meta App
        # dashboard: set the callback URL + verify token below and subscribe the
        # Instagram account to the `messages` field. No server-side call needed.
        return WebhookRegisterOut(
            webhook_url=webhook_url,
            ok=True,
            detail=(
                "Set this URL + verify token in the Meta App dashboard (Instagram "
                "webhooks) and subscribe the account to the 'messages' field."
            ),
        )

    if integration.platform == "website":
        # Nothing to register externally — the on-site widget POSTs here directly.
        return WebhookRegisterOut(
            webhook_url=webhook_url,
            ok=True,
            detail="Point your website widget's POST requests to this URL.",
        )

    raise errors.webhook_not_supported(integration.platform)
