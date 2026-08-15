"""The durable job queue."""

from datetime import timedelta

import pytest

from app.models.job import FAILED, PENDING, RUNNING, SUCCEEDED, Job
from app.core.clock import utcnow
from app.services import queue
from tests import ai, webhooks as wh


@pytest.fixture
def handler():
    """Register a throwaway job kind, recording each run."""
    runs = []

    def record(payload):
        runs.append(payload)
        if payload.get("explode"):
            raise RuntimeError("handler blew up")

    queue.HANDLERS["test_job"] = record
    yield runs
    queue.HANDLERS.pop("test_job", None)


def add(db, payload=None, **kw):
    job = queue.enqueue(db, "test_job", payload or {}, **kw)
    db.commit()
    db.refresh(job)
    return job


def test_a_job_survives_as_a_row(db, handler):
    job = add(db, {"hello": "world"})
    assert job.status == PENDING
    assert job.attempts == 0
    assert db.query(Job).count() == 1


def test_running_a_job_marks_it_succeeded(db, handler):
    add(db, {"hello": "world"})

    assert queue.run_once(db) is True

    assert handler == [{"hello": "world"}]
    job = db.query(Job).one()
    assert job.status == SUCCEEDED
    assert job.attempts == 1
    assert job.locked_at is None


def test_an_empty_queue_reports_nothing_to_do(db, handler):
    assert queue.run_once(db) is False


def test_a_failure_is_retried_later_not_immediately(db, handler):
    add(db, {"explode": True})

    queue.run_once(db)

    job = db.query(Job).one()
    assert job.status == PENDING, "must go back on the queue"
    assert job.attempts == 1
    assert "handler blew up" in job.last_error
    assert job.run_after > utcnow(), "backoff should defer the retry"
    # And it is not eligible yet, so a second pass finds nothing.
    assert queue.run_once(db) is False


def test_backoff_grows(db):
    assert queue._backoff(1) < queue._backoff(3) < queue._backoff(5)
    assert queue._backoff(20) == timedelta(seconds=600), "and is capped"


def test_a_job_gives_up_after_its_attempts(db, handler):
    add(db, {"explode": True}, max_attempts=3)

    for _ in range(3):
        job = db.query(Job).one()
        job.run_after = utcnow() - timedelta(seconds=1)
        db.commit()
        queue.run_once(db)

    job = db.query(Job).one()
    assert job.status == FAILED
    assert job.attempts == 3
    assert len(handler) == 3


def test_an_unknown_kind_fails_rather_than_looping(db):
    queue.enqueue(db, "no-such-kind", {})
    db.commit()

    queue.run_once(db)

    job = db.query(Job).one()
    assert job.status == FAILED
    assert "No handler" in job.last_error


def test_work_left_by_a_dead_worker_is_released(db, handler):
    """A process killed mid-job leaves the row RUNNING with nothing to finish
    it. Nothing else would ever pick that up."""
    job = add(db)
    job.status = RUNNING
    job.locked_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert queue.run_once(db) is False, "still leased, so not runnable"
    assert queue.release_stuck(db) == 1

    assert db.query(Job).one().status == PENDING
    assert queue.run_once(db) is True


def test_a_freshly_claimed_job_is_left_alone(db, handler):
    job = add(db)
    job.status = RUNNING
    job.locked_at = utcnow()
    db.commit()

    assert queue.release_stuck(db) == 0


def test_a_claimed_job_is_not_handed_out_twice(db, handler):
    """What SKIP LOCKED buys: the claim flips the row out of PENDING before
    the handler runs, so a second caller sees nothing to take."""
    add(db)
    claimed = queue._claim(db)

    assert claimed is not None
    assert queue._claim(db) is None


def test_jobs_run_oldest_first(db, handler):
    add(db, {"n": 1})
    add(db, {"n": 2})
    db.query(Job).filter(Job.payload["n"].astext == "1").update(
        {Job.run_after: utcnow() - timedelta(minutes=5)},
        synchronize_session=False)
    db.commit()

    queue.drain()

    db.expire_all()
    assert [r["n"] for r in handler] == [1, 2]


def test_drain_stops_at_its_limit(db, handler):
    for i in range(5):
        add(db, {"n": i})

    assert queue.drain(limit=2) == 2
    assert len(handler) == 2


def test_finished_jobs_are_pruned_after_retention(db, handler, monkeypatch):
    old = add(db)
    queue.run_once(db)
    old.updated_at = utcnow() - timedelta(days=8)
    fresh = add(db)
    queue.run_once(db)
    pending = add(db)
    pending.updated_at = utcnow() - timedelta(days=8)
    db.commit()
    monkeypatch.setattr(queue.settings, "JOB_RETENTION_DAYS", 7)

    assert queue.prune_finished(db) == 1
    assert {job.id for job in db.query(Job).all()} == {fresh.id, pending.id}


# ── The webhook path ─────────────────────────────────────────────────────────

def test_a_webhook_persists_its_work_before_answering(client, seller, db, monkeypatch):
    """With no inline runner, the reply has not happened when the platform is
    acknowledged — but the job is on disk, so a restart cannot lose it."""
    monkeypatch.setattr(queue.settings, "JOB_RUNNER", "worker")
    wh.connect(client, seller, external_id="page-1")

    with wh.sends() as sent, ai.replies("later"):
        r = wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))

    assert r.status_code == 200
    assert sent == [], "nothing sent yet"
    job = db.query(Job).one()
    assert job.kind == "inbound_message"
    assert job.status == PENDING
    assert job.payload["message"]["text"] == "hi"

    # A worker picking it up afterwards completes the exchange.
    with wh.sends() as sent, ai.replies("later"):
        queue.drain()

    assert sent[0]["text"] == "later"
    # drain() commits from a session of its own, so this one still holds the
    # row as it was read above.
    db.expire_all()
    assert db.query(Job).one().status == SUCCEEDED


def test_replaying_a_job_does_not_double_reply(client, seller, db, monkeypatch):
    """release_stuck can hand back a job that had already done its work, so the
    handler has to be safe to run twice."""
    monkeypatch.setattr(queue.settings, "JOB_RUNNER", "worker")
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies("once"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "hi"))

    with wh.sends(), ai.replies("once"):
        queue.drain()

    job = db.query(Job).one()
    job.status = PENDING
    db.commit()
    with wh.sends() as second, ai.replies("twice"):
        queue.drain()

    assert second == [], "the redelivery guard should recognise the stored message"
    conv = client.get("/api/v1/conversations/", headers=seller.headers).json()[0]
    thread = client.get(f"/api/v1/conversations/{conv['id']}/messages",
                        headers=seller.headers).json()
    assert [m["sender_type"] for m in thread] == ["customer", "assistant"]
