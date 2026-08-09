"""Outbound email.

Deliberately provider-agnostic: SES, Resend, Postmark and Mailgun all expose an
SMTP endpoint, so choosing one is a matter of setting SMTP_* rather than a code
change. With SMTP_HOST unset the message is logged instead of sent, which keeps
local development free of a mail account.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> None:
    """Send one plain-text email.

    Never raises: callers run inside request handlers whose response must not
    depend on whether the mail server is reachable, and for the password reset
    and OTP flows a failure here must not change what the client is told.
    """
    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP_HOST is not configured; email not sent.\n"
            "  To: %s\n  Subject: %s\n\n%s",
            to, subject, body,
        )
        return

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(message)
    except Exception:
        # Logged rather than raised: telling the caller that delivery failed
        # would reveal that the address belongs to a real account.
        logger.exception("Failed to send email to %s", to)


def send_password_reset(to: str, token: str) -> None:
    link = f"{settings.APP_BASE_URL.rstrip('/')}/reset-password?token={token}"
    send_email(
        to,
        "Reset your Apsara Assistant password",
        f"""Someone asked to reset the password for this Apsara Assistant account.

Open this link to choose a new one:

{link}

The link expires in {settings.PASSWORD_RESET_EXPIRE_MINUTES} minutes and can be
used once. If you did not ask for this, you can ignore this email — your
password will not change.
""",
    )


def send_login_otp(to: str, code: str) -> None:
    send_email(
        to,
        f"{code} is your Apsara Assistant sign-in code",
        f"""Your one-time sign-in code is:

    {code}

It expires in {settings.OTP_EXPIRE_MINUTES} minutes and can be used once. If
you did not try to sign in, you can ignore this email.
""",
    )
