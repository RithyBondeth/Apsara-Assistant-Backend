"""Background worker.

    python -m app.worker

Runs alongside the API when JOB_RUNNER=worker. Several may run at once — jobs
are claimed with SKIP LOCKED, so they will not collide.
"""

import logging
import signal
import time

from app.core.config import settings
from app.database import SessionLocal

# Importing for the side effect of registering the handlers on the queue.
from app.services import inbound  # noqa: F401
from app.services import throttle
from app.services.queue import release_stuck, run_once

from app.core.logging import configure as configure_logging
from app.core.observability import configure as configure_error_tracking

configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
# Worth more here than in the API: these failures happen with no user watching.
configure_error_tracking()

logger = logging.getLogger("app.worker")

_running = True


def _stop(signum, _frame):
    """Finish the job in hand rather than abandoning it mid-flight."""
    global _running
    logger.info("Signal %s received; stopping after the current job", signum)
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("Worker started; polling every %ss", settings.JOB_POLL_SECONDS)
    last_reap = 0.0

    while _running:
        db = SessionLocal()
        try:
            # Periodically return jobs left RUNNING by a process that died, and
            # drop failed sign-ins too old to throttle anything.
            if time.monotonic() - last_reap > settings.JOB_LEASE_SECONDS:
                release_stuck(db)
                throttle.prune(db)
                last_reap = time.monotonic()

            worked = run_once(db)
        except Exception:
            # A failure here is the loop itself, not a job — keep going rather
            # than exiting and leaving the queue unattended.
            logger.exception("Worker loop error")
            worked = False
        finally:
            db.close()

        if not worked:
            time.sleep(settings.JOB_POLL_SECONDS)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
