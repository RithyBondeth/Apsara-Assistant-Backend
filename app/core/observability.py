"""Error tracking.

Deliberately optional and off by default: with no SENTRY_DSN set this does
nothing, so local development and the test suite report nowhere. The dependency
is also optional — if sentry-sdk is not installed, the app still starts.

This matters more than it did before webhooks. Failures now happen in a worker
with no user watching: a reply a platform refused, a job that exhausted its
retries. Nobody finds out unless something reports it.
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def configure() -> bool:
    """Start error reporting if it is configured. Returns whether it is on."""
    if not settings.SENTRY_DSN:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; "
            "errors will only be logged. Install it with `pip install sentry-sdk`."
        )
        return False

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        # Traces are sampled separately and default to off: they are the
        # expensive part, and errors are what is wanted here.
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # Customer messages and seller credentials pass through this app; none
        # of it belongs in a third-party error report.
        send_default_pii=False,
    )
    logger.info("Error tracking enabled for environment %r", settings.ENVIRONMENT)
    return True
