"""Background worker.

    python -m app.worker

Runs alongside the API when JOB_RUNNER=worker. Several may run at once — jobs
are claimed with SKIP LOCKED, so they will not collide.
"""

import logging
import signal
import threading

from app.core.config import settings

# Importing for the side effect of registering the handlers on the queue.
from app.services import inbound  # noqa: F401
from app.services import alerts  # noqa: F401
from app.services import seller_alerts  # noqa: F401
from app.services import receipts  # noqa: F401
from app.services.queue import work

from app.core.logging import configure as configure_logging
from app.core.observability import configure as configure_error_tracking

configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
# Worth more here than in the API: these failures happen with no user watching.
configure_error_tracking()

logger = logging.getLogger("app.worker")

_stop = threading.Event()


def _on_signal(signum, _frame):
    """Finish the job in hand rather than abandoning it mid-flight."""
    logger.info("Signal %s received; stopping after the current job", signum)
    _stop.set()


def main() -> None:
    settings.check_media_storage()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    logger.info("Worker started; polling every %ss", settings.JOB_POLL_SECONDS)
    work(_stop)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
