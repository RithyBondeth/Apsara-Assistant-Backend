"""Auth endpoint tests."""
from __future__ import annotations

import pytest

from app.api.v1.endpoints import auth as auth_ep
from app.services import email


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # The send-limiter is process-global; clear it around each test so throttling
    # from one test doesn't leak 429s into the next.
    auth_ep._send_limiter.clear()
    yield
    auth_ep._send_limiter.clear()


@pytest.fixture()
def outbox(monkeypatch):
    """Capture outbound reset links / OTP codes instead of sending them."""
    box: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        email, "send_password_reset_email", lambda to, url: box.append(("reset", to, url))
    )
    monkeypatch.setattr(email, "send_otp_email", lambda to, code: box.append(("otp", to, code)))
    return box


def _register(client, email_addr="user@example.com", password="password123"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email_addr, "password": password, "full_name": "U"},
    )
    assert r.status_code == 201, r.text
    return email_addr


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_register_and_login_flow(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "a@b.com", "password": "password123", "full_name": "A"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "a@b.com"
    assert "password" not in r.json() and "password_hash" not in r.json()

    r = client.post(
        "/api/v1/auth/login",
        data={"username": "a@b.com", "password": "password123"},
    )
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"


def test_register_duplicate_email_rejected(client):
    body = {"email": "dup@b.com", "password": "password123", "full_name": "A"}
    assert client.post("/api/v1/auth/register", json=body).status_code == 201
    assert client.post("/api/v1/auth/register", json=body).status_code == 400


def test_login_wrong_password(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": "c@b.com", "password": "password123", "full_name": "C"},
    )
    r = client.post(
        "/api/v1/auth/login", data={"username": "c@b.com", "password": "wrong"}
    )
    assert r.status_code == 401


def test_me_requires_auth(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_current_user(auth_client):
    client, _token, _uid = auth_client
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == "seller@example.com"


def test_change_password(auth_client):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "supersecret", "new_password": "brandnewpass"},
    )
    assert r.status_code == 204

    # old password no longer works, new one does
    assert client.post(
        "/api/v1/auth/login",
        data={"username": "seller@example.com", "password": "supersecret"},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        data={"username": "seller@example.com", "password": "brandnewpass"},
    ).status_code == 200


def test_change_password_wrong_current(auth_client):
    client, _token, _uid = auth_client
    r = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "nope", "new_password": "brandnewpass"},
    )
    assert r.status_code == 400


# ── Forgot / reset password ──────────────────────────────────────────────────


def test_forgot_password_sends_link_and_reset_works(client, outbox):
    _register(client, "reset@b.com", "password123")

    r = client.post("/api/v1/auth/forgot-password", json={"email": "reset@b.com"})
    assert r.status_code == 200
    assert len(outbox) == 1
    kind, to, url = outbox[0]
    assert kind == "reset" and to == "reset@b.com"
    token = url.split("token=")[1]

    r = client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "brandnewpass"},
    )
    assert r.status_code == 200

    # New password works; old one no longer does.
    assert client.post(
        "/api/v1/auth/login",
        data={"username": "reset@b.com", "password": "brandnewpass"},
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/login",
        data={"username": "reset@b.com", "password": "password123"},
    ).status_code == 401


def test_forgot_password_unknown_email_is_silent(client, outbox):
    r = client.post("/api/v1/auth/forgot-password", json={"email": "nobody@b.com"})
    assert r.status_code == 200  # same response as a known address
    assert outbox == []  # but nothing is actually sent


def test_reset_token_is_single_use(client, outbox):
    _register(client, "once@b.com", "password123")
    client.post("/api/v1/auth/forgot-password", json={"email": "once@b.com"})
    token = outbox[0][2].split("token=")[1]

    assert client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "brandnewpass"},
    ).status_code == 200
    # Re-using the same token is rejected.
    assert client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "anotherpass1"},
    ).status_code == 400


def test_reset_with_invalid_token_rejected(client):
    r = client.post(
        "/api/v1/auth/reset-password",
        json={"token": "not-a-real-token", "new_password": "brandnewpass"},
    )
    assert r.status_code == 400


# ── OTP login ────────────────────────────────────────────────────────────────


def test_otp_login_flow(client, outbox):
    _register(client, "otp@b.com", "password123")

    r = client.post("/api/v1/auth/otp/request", json={"email": "otp@b.com"})
    assert r.status_code == 200
    kind, to, code = outbox[0]
    assert kind == "otp" and to == "otp@b.com" and len(code) == 6

    r = client.post("/api/v1/auth/otp/verify", json={"email": "otp@b.com", "code": code})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"


def test_otp_code_is_single_use(client, outbox):
    _register(client, "otp2@b.com", "password123")
    client.post("/api/v1/auth/otp/request", json={"email": "otp2@b.com"})
    code = outbox[0][2]

    assert client.post(
        "/api/v1/auth/otp/verify", json={"email": "otp2@b.com", "code": code}
    ).status_code == 200
    # Second use of the same code fails.
    assert client.post(
        "/api/v1/auth/otp/verify", json={"email": "otp2@b.com", "code": code}
    ).status_code == 401


def test_otp_wrong_code_rejected(client, outbox):
    _register(client, "otp3@b.com", "password123")
    client.post("/api/v1/auth/otp/request", json={"email": "otp3@b.com"})

    r = client.post("/api/v1/auth/otp/verify", json={"email": "otp3@b.com", "code": "000000"})
    assert r.status_code == 401


def test_otp_burns_after_max_attempts(client, outbox):
    from app.core.config import settings

    _register(client, "otp4@b.com", "password123")
    client.post("/api/v1/auth/otp/request", json={"email": "otp4@b.com"})
    real_code = outbox[0][2]
    wrong = "111111" if real_code != "111111" else "222222"

    for _ in range(settings.OTP_MAX_ATTEMPTS):
        assert client.post(
            "/api/v1/auth/otp/verify", json={"email": "otp4@b.com", "code": wrong}
        ).status_code == 401

    # Even the correct code no longer works once the code is burned.
    assert client.post(
        "/api/v1/auth/otp/verify", json={"email": "otp4@b.com", "code": real_code}
    ).status_code == 401


def test_otp_verify_unknown_email_rejected(client):
    r = client.post("/api/v1/auth/otp/verify", json={"email": "ghost@b.com", "code": "123456"})
    assert r.status_code == 401


def test_send_endpoints_are_rate_limited(client, outbox):
    from app.core.config import settings

    # Exhaust the per-IP window, then expect a 429.
    for _ in range(settings.AUTH_RATE_LIMIT):
        assert client.post(
            "/api/v1/auth/forgot-password", json={"email": "nobody@b.com"}
        ).status_code == 200
    assert client.post(
        "/api/v1/auth/forgot-password", json={"email": "nobody@b.com"}
    ).status_code == 429
