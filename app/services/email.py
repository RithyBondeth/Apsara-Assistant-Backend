"""Transactional email delivery.

When SMTP is configured (``SMTP_HOST`` set) messages are sent over SMTP.
Otherwise — the default for local dev — they are logged at INFO so flows like
password reset and OTP login are fully testable without an email provider.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> None:
    if not settings.smtp_enabled:
        # Dev fallback: no SMTP configured, so surface the message in the logs.
        logger.info("[email:dev] to=%s subject=%s\n%s", to, subject, body)
        return

    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        if settings.SMTP_USE_TLS:
            server.starttls()
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)

    logger.info("Sent %r email to %s", subject, to)


def send_password_reset_email(to: str, reset_url: str) -> None:
    send_email(
        to,
        subject="Reset your Apsara password",
        body=(
            "We received a request to reset your Apsara Assistant password.\n\n"
            f"Reset it here (the link expires in "
            f"{settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES} minutes):\n{reset_url}\n\n"
            "If you didn't request this, you can safely ignore this email."
        ),
    )


def send_otp_email(to: str, code: str) -> None:
    send_email(
        to,
        subject="Your Apsara sign-in code",
        body=(
            f"Your one-time sign-in code is: {code}\n\n"
            f"It expires in {settings.OTP_EXPIRE_MINUTES} minutes. "
            "If you didn't request it, you can ignore this email."
        ),
    )
