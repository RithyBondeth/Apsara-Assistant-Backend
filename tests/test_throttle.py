"""Sign-in throttling: how many password guesses `/auth/login` will accept."""

import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.config import settings
from app.models.login_attempt import LoginAttempt
from app.services import throttle
from tests.conftest import PASSWORD, register


def login(client, email, password):
    return client.post("/api/v1/auth/login",
                       data={"username": email, "password": password})


def exhaust(client, email, times=None):
    """Spend the per-account allowance on wrong passwords."""
    for _ in range(times if times is not None else settings.LOGIN_MAX_ATTEMPTS):
        login(client, email, "wrong-password")


@pytest.fixture
def small_limit(monkeypatch):
    """A ceiling low enough to reach without a hundred bcrypt comparisons."""
    monkeypatch.setattr(settings, "LOGIN_MAX_ATTEMPTS", 3)
    return 3


# ── The ceiling ──────────────────────────────────────────────────────────────

def test_guessing_is_refused_once_the_ceiling_is_reached(client, small_limit):
    seller = register(client)
    exhaust(client, seller.email, small_limit)

    r = login(client, seller.email, "wrong-password")

    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_the_ceiling_holds_even_with_the_right_password(client, small_limit):
    """The whole point: a throttled caller cannot test a correct guess either.

    If the correct password still got through, an attacker's last guess would
    always be free and the limit would protect nothing.
    """
    seller = register(client)
    exhaust(client, seller.email, small_limit)

    assert login(client, seller.email, PASSWORD).status_code == 429


def test_attempts_below_the_ceiling_still_get_a_401(client, small_limit):
    seller = register(client)
    for _ in range(small_limit - 1):
        assert login(client, seller.email, "wrong-password").status_code == 401


def test_signing_in_successfully_clears_the_slate(client, small_limit):
    """A seller who fumbles their password and then remembers it is not locked
    out for the rest of the window."""
    seller = register(client)
    exhaust(client, seller.email, small_limit - 1)

    assert login(client, seller.email, PASSWORD).status_code == 200
    # The earlier failures are gone, so the full allowance is available again.
    exhaust(client, seller.email, small_limit - 1)
    assert login(client, seller.email, PASSWORD).status_code == 200


def test_attempts_outside_the_window_no_longer_count(client, small_limit, db):
    seller = register(client)
    exhaust(client, seller.email, small_limit)
    assert login(client, seller.email, PASSWORD).status_code == 429

    for row in db.query(LoginAttempt).filter(LoginAttempt.email == seller.email):
        row.created_at = utcnow() - settings.login_attempt_window - timedelta(minutes=1)
    db.commit()

    assert login(client, seller.email, PASSWORD).status_code == 200


# ── What the throttle must not give away ─────────────────────────────────────

def test_unknown_addresses_are_throttled_too(client, small_limit):
    """Otherwise the 429 itself would reveal which emails have accounts.

    Everything else in auth is careful to say the same thing whether or not an
    address is registered; throttling only real accounts would undo that.
    """
    unknown = f"{uuid.uuid4().hex[:12]}@example.com"
    exhaust(client, unknown, small_limit)

    assert login(client, unknown, "wrong-password").status_code == 429


def test_one_account_cannot_lock_out_another(client, small_limit):
    victim = register(client)
    other = register(client)
    exhaust(client, other.email, small_limit)

    assert login(client, victim.email, PASSWORD).status_code == 200


def test_casing_does_not_multiply_the_allowance(client, small_limit):
    seller = register(client)
    for i in range(small_limit):
        variant = seller.email.upper() if i % 2 else seller.email
        login(client, variant, "wrong-password")

    assert login(client, seller.email, PASSWORD).status_code == 429


# ── Configuration and housekeeping ───────────────────────────────────────────

def test_a_zero_limit_disables_throttling(client, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_ATTEMPTS", 0)
    seller = register(client)
    exhaust(client, seller.email, 12)

    assert login(client, seller.email, PASSWORD).status_code == 200


def test_forwarded_headers_are_ignored_unless_proxies_are_trusted():
    """An untrusted X-Forwarded-For is a free way around the per-address
    ceiling, so it is only read when a proxy is declared."""
    class Request:
        headers = {"X-Forwarded-For": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert throttle.client_ip(Request()) == "10.0.0.1"


def test_forwarded_headers_are_used_when_proxies_are_trusted(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)

    class Request:
        headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert throttle.client_ip(Request()) == "1.2.3.4"


def test_prune_drops_only_attempts_past_the_window(db):
    throttle.record_failure(db, "old@example.com", None)
    throttle.record_failure(db, "recent@example.com", None)
    old = db.query(LoginAttempt).filter(LoginAttempt.email == "old@example.com").first()
    old.created_at = utcnow() - settings.login_attempt_window - timedelta(minutes=1)
    db.commit()

    assert throttle.prune(db) == 1
    remaining = [a.email for a in db.query(LoginAttempt)]
    assert remaining == ["recent@example.com"]
