"""JOB_RUNNER=inline: jobs run on a thread of the API, never in the request.

A webhook has to be answered promptly or the platform retries it, and the job
behind one includes an OpenAI round trip. These tests hold a job open and check
that neither the webhook that queued it nor anything else waits for it.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.job import PENDING, SUCCEEDED, Job
from app.services import queue
from app.services.inbound import INBOUND_MESSAGE
from app.services.queue import start_drainer, stop_drainer
from tests import webhooks as wh


@pytest.fixture
def blocking_inbound(monkeypatch):
    """Replace the inbound handler with one that waits to be released."""
    started, release = threading.Event(), threading.Event()

    def handler(payload):
        started.set()
        release.wait(10)

    monkeypatch.setitem(queue.HANDLERS, INBOUND_MESSAGE, handler)
    yield started, release
    release.set()


@pytest.fixture
def drainer(monkeypatch):
    """Start the real drainer on demand, and always stop it."""
    # A long poll, so a prompt pickup can only come from the wake on commit.
    monkeypatch.setattr(queue.settings, "JOB_POLL_SECONDS", 30.0)
    yield start_drainer
    stop_drainer()


def _job_status(db):
    db.expire_all()
    return db.query(Job).one().status


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_the_request_never_runs_the_job_it_queued(client, seller, db, blocking_inbound):
    """With no drainer to race it, anything that ran the job did so in the
    request — which is what made a webhook wait on its OpenAI call."""
    started, _ = blocking_inbound
    wh.connect(client, seller, external_id="page-1")

    # The app itself, without the test client's drain-after-request stand-in.
    with TestClient(app) as raw:
        began = time.perf_counter()
        r = wh.post_messenger(raw, wh.messenger_payload("page-1", "psid-1", "hi"))
        answered_in = time.perf_counter() - began
        raw.get("/health")

    assert r.status_code == 200
    assert not started.is_set(), "the job ran inside a request"
    assert answered_in < 1.0, f"the webhook waited {answered_in:.1f}s"
    assert _job_status(db) == PENDING


def test_the_drainer_runs_jobs_without_holding_up_requests(
        client, seller, db, blocking_inbound, drainer):
    started, release = blocking_inbound
    wh.connect(client, seller, external_id="page-1")
    drainer()

    with TestClient(app) as raw:
        # Posted once the drainer is idle in a 30s poll: a prompt pickup can
        # only be the wake on commit.
        time.sleep(0.2)
        r = wh.post_messenger(raw, wh.messenger_payload("page-1", "psid-1", "hi"))
        assert r.status_code == 200
        assert started.wait(5), "the drainer should be woken by the commit"

        # The job is mid-flight; an unrelated request must not queue behind it.
        began = time.perf_counter()
        assert raw.get("/health").status_code == 200
        assert time.perf_counter() - began < 1.0

    release.set()
    assert _wait_for(lambda: _job_status(db) == SUCCEEDED)


@pytest.mark.parametrize("runner, starts", [("inline", 1), ("worker", 0)])
def test_only_an_inline_api_starts_the_drainer(monkeypatch, runner, starts):
    calls = []
    monkeypatch.setattr(queue.settings, "JOB_RUNNER", runner)
    monkeypatch.setattr(queue, "start_drainer", lambda: calls.append(1))

    with TestClient(app):
        pass

    assert len(calls) == starts
