"""Sign-in throttling.

The login endpoint is public and runs bcrypt, so it is both a credential-guessing
target and a CPU-exhaustion one. These tests pin the two buckets (per account,
per IP) and the reset-on-success behaviour.

Note the limiters are cleared between tests by an autouse fixture in conftest.
"""
from __future__ import annotations

import pytest

from app.api.v1.endpoints import auth as auth_ep

GOOD = "password123"
BAD = "wrong-password"


@pytest.fixture()
def small_limits(monkeypatch):
    """Shrink the windows so a test can exhaust them in a few calls.

    Rebuilding the limiter objects (rather than editing settings, which the
    module read at import time) is what actually takes effect.
    """
    from app.core.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(auth_ep, "_login_account_limiter", SlidingWindowRateLimiter(3, 300))
    monkeypatch.setattr(auth_ep, "_login_ip_limiter", SlidingWindowRateLimiter(5, 300))
    monkeypatch.setattr(auth_ep, "_register_limiter", SlidingWindowRateLimiter(2, 3600))


def _register(client, email_addr="seller@example.com"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email_addr, "password": GOOD, "full_name": "S"},
    )
    assert r.status_code == 201, r.text


def _login(client, email_addr="seller@example.com", password=GOOD):
    return client.post(
        "/api/v1/auth/login", data={"username": email_addr, "password": password}
    )


def test_repeated_wrong_passwords_are_throttled(client, small_limits):
    _register(client)

    for _ in range(3):
        assert _login(client, password=BAD).status_code == 401

    # Fourth attempt never reaches bcrypt.
    r = _login(client, password=BAD)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "too_many_requests"


def test_throttle_blocks_the_correct_password_too(client, small_limits):
    """A guesser who lands on the right password after the window closes still loses.

    If the throttle only rejected *wrong* passwords it would be decorative — the
    attacker's winning attempt is by definition a correct one.
    """
    _register(client)
    for _ in range(3):
        _login(client, password=BAD)

    assert _login(client, password=GOOD).status_code == 429


def test_successful_login_clears_the_account_bucket(client, small_limits, monkeypatch):
    """Fumbling the password then getting it right must not leave you near a 429."""
    from app.core.rate_limit import SlidingWindowRateLimiter

    # Widen the IP bucket so it can't be what stops us — this test is about the
    # account bucket alone, and the 6 requests below would otherwise exhaust the
    # shared IP ceiling first and pass/fail for the wrong reason.
    monkeypatch.setattr(auth_ep, "_login_ip_limiter", SlidingWindowRateLimiter(100, 300))

    _register(client)
    for _ in range(2):
        assert _login(client, password=BAD).status_code == 401

    assert _login(client, password=GOOD).status_code == 200

    # Budget restored: three more failures are absorbed rather than the one that
    # would have been left had the successful login not reset the bucket.
    for _ in range(3):
        assert _login(client, password=BAD).status_code == 401


def test_account_bucket_is_case_insensitive(client, small_limits):
    """Varying the case of the email must not mint a fresh budget."""
    _register(client)
    for variant in ("seller@example.com", "Seller@Example.com", "SELLER@EXAMPLE.COM"):
        assert _login(client, email_addr=variant, password=BAD).status_code == 401

    assert _login(client, email_addr="sElLeR@example.com", password=BAD).status_code == 429


def test_ip_bucket_caps_guessing_across_many_accounts(client, small_limits):
    """Spreading attempts over different emails still hits the per-IP ceiling.

    Each email has its own account bucket (limit 3), so with only that bucket
    this would never throttle — the IP bucket (limit 5) is what stops it.
    """
    codes = [
        _login(client, email_addr=f"victim{i}@example.com", password=BAD).status_code
        for i in range(7)
    ]
    # Unknown accounts 401 until the IP ceiling of 5 is reached.
    assert codes[:5] == [401] * 5
    assert codes[5:] == [429, 429]


def test_unknown_account_is_throttled_like_a_real_one(client, small_limits):
    """The 401/429 boundary must not differ by whether the email is registered.

    If a registered address throttled at a different point than an unregistered
    one, the throttle itself would become an account-enumeration oracle.
    """
    for _ in range(3):
        assert _login(client, email_addr="nobody@example.com", password=BAD).status_code == 401
    assert _login(client, email_addr="nobody@example.com", password=BAD).status_code == 429


def test_register_is_throttled_per_ip(client, small_limits):
    for i in range(2):
        r = client.post(
            "/api/v1/auth/register",
            json={"email": f"new{i}@example.com", "password": GOOD, "full_name": "N"},
        )
        assert r.status_code == 201

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "new2@example.com", "password": GOOD, "full_name": "N"},
    )
    assert r.status_code == 429


def test_forwarded_ip_is_used_as_the_key(client, small_limits):
    """Behind a proxy every request carries the proxy's IP, so the throttle must
    key on X-Forwarded-For or it would throttle all sellers as one client."""
    for _ in range(5):
        client.post(
            "/api/v1/auth/login",
            data={"username": "a@example.com", "password": BAD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    blocked = client.post(
        "/api/v1/auth/login",
        data={"username": "a@example.com", "password": BAD},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert blocked.status_code == 429

    other = client.post(
        "/api/v1/auth/login",
        data={"username": "b@example.com", "password": BAD},
        headers={"X-Forwarded-For": "198.51.100.4"},
    )
    assert other.status_code == 401
