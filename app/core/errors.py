"""Translatable API errors.

Every seller-facing error is raised as a stable ``code`` plus its parameters,
rather than a finished English sentence. The frontend already carries a full
en/km translation system, so it renders the code; the backend never needs a
second message catalogue and Khmer copy stays in one repo.

The response body keeps an English ``message`` alongside the code. That is
deliberate and not redundant:

* a bare code is useless to anyone reading logs or hitting the API directly;
* the frontend needs a fallback for a code it doesn't recognise yet, which
  happens whenever the backend deploys ahead of the web app. Without it, a new
  error would render as a blank box.

Errors that are consumed by a PLATFORM rather than a seller — webhook signature
and verification failures — deliberately stay plain ``HTTPException``s. Telegram
and Meta read those, and no human ever sees them in the dashboard.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


class ApiError(HTTPException):
    """An error the UI can translate.

    ``detail`` is a dict rather than a string, so callers must be prepared for
    both shapes — FastAPI's own validation errors still use the string/list
    form. The frontend's ``extractErrorMessage`` handles that.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        **params: Any,
    ) -> None:
        self.code = code
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message, "params": params},
        )


# ── Auth ──────────────────────────────────────────────────────────────────────

def email_already_registered() -> ApiError:
    return ApiError(code="email_already_registered", message="Email already registered")


def invalid_credentials() -> ApiError:
    return ApiError(
        code="invalid_credentials",
        message="Invalid credentials",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def account_inactive() -> ApiError:
    return ApiError(
        code="account_inactive",
        message="Account is inactive",
        status_code=status.HTTP_403_FORBIDDEN,
    )


def current_password_incorrect() -> ApiError:
    return ApiError(code="current_password_incorrect", message="Current password is incorrect")


def invalid_or_expired_code() -> ApiError:
    return ApiError(
        code="invalid_or_expired_code",
        message="Invalid or expired code",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def invalid_or_expired_reset_link() -> ApiError:
    return ApiError(
        code="invalid_or_expired_reset_link",
        message="This reset link is invalid or has expired",
    )


def too_many_requests() -> ApiError:
    return ApiError(
        code="too_many_requests",
        message="Too many requests — please try again later",
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def invalid_token() -> ApiError:
    return ApiError(
        code="invalid_token",
        message="Invalid token",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def user_not_found() -> ApiError:
    return ApiError(
        code="user_not_found",
        message="User not found",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


# ── Products ──────────────────────────────────────────────────────────────────

def product_not_found() -> ApiError:
    return ApiError(
        code="product_not_found",
        message="Product not found",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def product_unavailable(name: str) -> ApiError:
    return ApiError(
        code="product_unavailable",
        message=f"Product '{name}' is not available",
        name=name,
    )


def insufficient_stock(name: str, available: int) -> ApiError:
    return ApiError(
        code="insufficient_stock",
        message=f"Insufficient stock for '{name}' (available: {available})",
        name=name,
        available=available,
    )


# ── Customers ─────────────────────────────────────────────────────────────────

def customer_not_found() -> ApiError:
    return ApiError(
        code="customer_not_found",
        message="Customer not found",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def unsupported_platform(platforms: str) -> ApiError:
    return ApiError(
        code="unsupported_platform",
        message=f"Unsupported platform. Supported: {platforms}",
        platforms=platforms,
    )


# ── Conversations ─────────────────────────────────────────────────────────────

def conversation_not_found() -> ApiError:
    return ApiError(
        code="conversation_not_found",
        message="Conversation not found",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def conversation_closed() -> ApiError:
    return ApiError(
        code="conversation_closed",
        message="Cannot send messages to a closed conversation",
    )


def message_empty() -> ApiError:
    return ApiError(code="message_empty", message="Message cannot be empty")


def delivery_failed(reason: str) -> ApiError:
    """The platform refused the seller's reply.

    ``reason`` is the platform's own text (Meta / Telegram), which is English
    and outside our control — so the translation wraps it rather than replacing
    it, and the seller still sees what the platform actually said.
    """
    return ApiError(
        code="delivery_failed",
        message=reason,
        status_code=status.HTTP_502_BAD_GATEWAY,
        reason=reason,
    )


def assistant_unavailable() -> ApiError:
    return ApiError(
        code="assistant_unavailable",
        message="Assistant is temporarily unavailable",
        status_code=status.HTTP_502_BAD_GATEWAY,
    )


# ── Orders ────────────────────────────────────────────────────────────────────

def order_not_found() -> ApiError:
    return ApiError(
        code="order_not_found",
        message="Order not found",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def order_empty() -> ApiError:
    return ApiError(code="order_empty", message="Order must contain at least one item")


def item_quantity_invalid() -> ApiError:
    return ApiError(code="item_quantity_invalid", message="Item quantity must be positive")


def invalid_order_status(statuses: str) -> ApiError:
    return ApiError(
        code="invalid_order_status",
        message=f"Invalid status. Must be one of: {statuses}",
        statuses=statuses,
    )


# ── Uploads ───────────────────────────────────────────────────────────────────

def uploads_not_configured() -> ApiError:
    return ApiError(
        code="uploads_not_configured",
        message="Image uploads are not configured on the server",
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def unsupported_image_type(types: str) -> ApiError:
    return ApiError(
        code="unsupported_image_type",
        message=f"Unsupported image type. Allowed: {types}",
        types=types,
    )


def image_too_large(limit: str) -> ApiError:
    return ApiError(
        code="image_too_large",
        message=f"Image exceeds the {limit} limit",
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        limit=limit,
    )


def empty_file() -> ApiError:
    return ApiError(code="empty_file", message="Empty file")


def upload_failed(reason: str) -> ApiError:
    return ApiError(
        code="upload_failed",
        message=f"Upload failed: {reason}",
        status_code=status.HTTP_502_BAD_GATEWAY,
        reason=reason,
    )


def delete_failed(reason: str) -> ApiError:
    return ApiError(
        code="delete_failed",
        message=f"Delete failed: {reason}",
        status_code=status.HTTP_502_BAD_GATEWAY,
        reason=reason,
    )


# ── Integrations ──────────────────────────────────────────────────────────────

def integration_not_found() -> ApiError:
    return ApiError(
        code="integration_not_found",
        message="Integration not found",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def integration_already_connected() -> ApiError:
    return ApiError(
        code="integration_already_connected",
        message="This platform account is already connected",
        status_code=status.HTTP_409_CONFLICT,
    )


def app_secret_required(platform: str) -> ApiError:
    return ApiError(
        code="app_secret_required",
        message=f"app_secret is required for {platform} integrations",
        platform=platform,
    )


def external_id_required() -> ApiError:
    return ApiError(
        code="external_id_required",
        message="external_id (Facebook Page id) is required to subscribe the page",
    )


def webhook_not_supported(platform: str) -> ApiError:
    return ApiError(
        code="webhook_not_supported",
        message=f"Webhook registration not supported for {platform}",
    )


def public_base_url_not_configured() -> ApiError:
    # Kept at 400 to match the pre-refactor behaviour. Arguably a 503 (the
    # server is misconfigured, not the request), but changing a status code is
    # an API contract change and doesn't belong in a translation refactor.
    return ApiError(
        code="public_base_url_not_configured",
        message="PUBLIC_BASE_URL is not configured on the server",
    )


# Every code the API can emit. The frontend must carry a translation for each;
# `tests/test_errors.py` pins this list so adding an error without registering
# it here (and therefore without a Khmer string) fails the suite.
ALL_ERROR_CODES = frozenset(
    {
        "email_already_registered",
        "invalid_credentials",
        "account_inactive",
        "current_password_incorrect",
        "invalid_or_expired_code",
        "invalid_or_expired_reset_link",
        "too_many_requests",
        "invalid_token",
        "user_not_found",
        "product_not_found",
        "product_unavailable",
        "insufficient_stock",
        "customer_not_found",
        "unsupported_platform",
        "conversation_not_found",
        "conversation_closed",
        "message_empty",
        "delivery_failed",
        "assistant_unavailable",
        "order_not_found",
        "order_empty",
        "item_quantity_invalid",
        "invalid_order_status",
        "uploads_not_configured",
        "unsupported_image_type",
        "image_too_large",
        "empty_file",
        "upload_failed",
        "delete_failed",
        "integration_not_found",
        "integration_already_connected",
        "app_secret_required",
        "external_id_required",
        "webhook_not_supported",
        "public_base_url_not_configured",
        # Raised by the validation handler in main.py, not a factory here.
        "validation_error",
    }
)
