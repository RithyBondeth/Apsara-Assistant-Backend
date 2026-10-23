"""A small durable job queue on top of Postgres.

Webhook handlers must answer quickly — a platform that does not get a prompt 2xx
retries — but the work they trigger includes an OpenAI round trip. Previously
that ran in a FastAPI BackgroundTask, which meant a restart mid-flight dropped a
customer's message on the floor with nothing left to show it had happened.

Jobs are rows, so they survive the process. Claiming uses FOR UPDATE SKIP
LOCKED, which lets any number of workers share the table without two of them
picking up the same row.
"""

import logging
import threading
import time
from datetime import timedelta
from typing import Callable

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.database import SessionLocal
from app.models.job import FAILED, PENDING, RUNNING, SUCCEEDED, Job
from app.services import storage

logger = logging.getLogger(__name__)

# kind -> handler. A handler takes the payload dict and raises to signal failure.
HANDLERS: dict[str, Callable[[dict], None]] = {}


def register(kind: str):
    def decorator(fn):
        HANDLERS[kind] = fn
        return fn
    return decorator


def enqueue(db: Session, kind: str, payload: dict, *, max_attempts: int = 5,
            run_after=None) -> Job:
    """Persist a job. Committed by the caller alongside whatever prompted it.
    `run_after` delays it — a poll scheduling its own next look."""
    job = Job(kind=kind, payload=payload, max_attempts=max_attempts,
              **({"run_after": run_after} if run_after is not None else {}))
    db.add(job)
    # Woken on commit rather than now: until then the row is invisible to the
    # drainer, which would find nothing and go back to sleep.
    if not db.info.get(_WAKE_ON_COMMIT):
        db.info[_WAKE_ON_COMMIT] = True
        event.listen(db, "after_commit", _wake_after_commit, once=True)
    return job


_WAKE_ON_COMMIT = "queue_wake_on_commit"


def _wake_after_commit(session: Session) -> None:
    session.info.pop(_WAKE_ON_COMMIT, None)
    wake()


def _backoff(attempts: int) -> timedelta:
    """Exponential, capped. A failing platform or model should be retried, but
    not at the same rate that produced the failure."""
    return timedelta(seconds=min(2 ** attempts * 5, 600))


def _claim(db: Session) -> Job | None:
    """Take one runnable job, or None.

    SKIP LOCKED rather than a plain FOR UPDATE: a second worker should move on
    to the next row instead of blocking behind the first.
    """
    row = db.execute(text("""
        SELECT id FROM jobs
        WHERE status = :pending AND run_after <= :now
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    """), {"pending": PENDING, "now": utcnow()}).first()
    if row is None:
        return None

    job = db.query(Job).filter(Job.id == row[0]).first()
    job.status = RUNNING
    job.locked_at = utcnow()
    job.attempts += 1
    db.commit()
    return job


def _finish(db: Session, job: Job, error: str | None, *, permanent: bool = False) -> None:
    if error is None:
        job.status = SUCCEEDED
        job.locked_at = None
    elif permanent or job.attempts >= job.max_attempts:
        job.status = FAILED
        job.locked_at = None
        job.last_error = error
        logger.error("Job %s (%s) gave up after %s attempts: %s",
                     job.id, job.kind, job.attempts, error)
    else:
        job.status = PENDING
        job.locked_at = None
        job.last_error = error
        job.run_after = utcnow() + _backoff(job.attempts)
    db.commit()


def run_once(db: Session) -> bool:
    """Run at most one job. Returns whether there was one."""
    job = _claim(db)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    if handler is None:
        # Retrying will not conjure a handler. Failing now keeps a typo'd or
        # retired job kind from occupying the queue through five backoffs.
        _finish(db, job, f"No handler registered for {job.kind!r}", permanent=True)
        return True

    try:
        handler(dict(job.payload))
    except Exception as exc:
        db.rollback()
        # Reloaded because the rollback detached the instance's state.
        job = db.query(Job).filter(Job.id == job.id).first()
        logger.exception("Job %s (%s) failed", job.id, job.kind)
        _finish(db, job, f"{type(exc).__name__}: {exc}"[:2000])
    else:
        _finish(db, job, None)
    return True


def drain(limit: int = 50) -> int:
    """Run up to `limit` jobs with a session of its own. Returns how many ran."""
    db = SessionLocal()
    try:
        ran = 0
        while ran < limit and run_once(db):
            ran += 1
        # Inline deployments have no worker loop to perform housekeeping.
        prune_finished(db)
        storage.sweep_tombstones(db)
        return ran
    finally:
        db.close()


def release_stuck(db: Session) -> int:
    """Return jobs abandoned by a dead worker to the queue.

    A process killed mid-job leaves its row RUNNING forever, since nothing is
    left to mark it either way. Anything held past the lease is assumed
    orphaned; the handler must therefore tolerate being run twice.
    """
    cutoff = utcnow() - timedelta(seconds=settings.JOB_LEASE_SECONDS)
    released = (
        db.query(Job)
        .filter(Job.status == RUNNING, Job.locked_at < cutoff)
        .update({Job.status: PENDING, Job.locked_at: None}, synchronize_session=False)
    )
    db.commit()
    if released:
        logger.warning("Released %s stuck job(s) back to the queue", released)
    return released


def prune_finished(db: Session) -> int:
    """Delete terminal jobs after their operational retention window."""
    if settings.JOB_RETENTION_DAYS <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.JOB_RETENTION_DAYS)
    deleted = (
        db.query(Job)
        .filter(Job.status.in_((SUCCEEDED, FAILED)), Job.updated_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info("Pruned %s finished job(s)", deleted)
    return deleted


# ── Running jobs ─────────────────────────────────────────────────────────────

def work(stop: threading.Event, wake_event: threading.Event | None = None) -> None:
    """Run jobs until `stop` is set. The loop behind both runners.

    Idle, it waits up to JOB_POLL_SECONDS — less when `wake_event` is set by
    a commit in the same process, so an inline deployment replies without
    waiting out the poll.
    """
    # Imported here: throttle is housekeeping, not something the queue needs
    # in order to be imported.
    from app.services import throttle

    last_reap = 0.0
    while not stop.is_set():
        db = SessionLocal()
        try:
            # Periodically return jobs left RUNNING by a process that died, and
            # drop failed sign-ins too old to throttle anything.
            if time.monotonic() - last_reap > settings.JOB_LEASE_SECONDS:
                release_stuck(db)
                throttle.prune(db)
                prune_finished(db)
                storage.sweep_tombstones(db)
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
            (wake_event or stop).wait(settings.JOB_POLL_SECONDS)
            if wake_event is not None:
                wake_event.clear()


# JOB_RUNNER=inline runs `work` on a thread of the API process. Not in the
# request: a webhook answered only after its own OpenAI round trip — or after
# everyone else's queued ahead of it — is a webhook the platform retries, and
# a synchronous drain in async middleware stalls every other request with it.
_stop = threading.Event()
_wake = threading.Event()
_drainer: threading.Thread | None = None


def wake() -> None:
    """Tell the in-process drainer there is work. Harmless when none runs."""
    _wake.set()


def start_drainer() -> None:
    global _drainer
    if _drainer is not None and _drainer.is_alive():
        return
    _stop.clear()
    _drainer = threading.Thread(target=work, args=(_stop, _wake),
                                name="job-drainer", daemon=True)
    _drainer.start()
    logger.info("In-process job drainer started")


def stop_drainer(timeout: float = 10.0) -> None:
    """Stop after the job in hand. One still running past `timeout` is left
    to the lease: the thread is a daemon, and the row is released later."""
    global _drainer
    if _drainer is None:
        return
    _stop.set()
    _wake.set()
    _drainer.join(timeout)
    _drainer = None
