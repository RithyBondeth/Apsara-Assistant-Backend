"""Registration, sign-in, password reset and one-time-code login."""

import hashlib
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.models.user import User
from app.models.verification_code import VerificationCode
from app.services import verification
from tests.conftest import PASSWORD, register


@pytest.fixture
def mail(monkeypatch):
    """Capture what would have been emailed, as (kind, address, secret)."""
    import app.api.v1.endpoints.auth as auth_ep

    sent = []
    monkeypatch.setattr(auth_ep, "send_password_reset",
                        lambda to, token: sent.append(("reset", to, token)))
    monkeypatch.setattr(auth_ep, "send_login_otp",
                        lambda to, code: sent.append(("otp", to, code)))
    return sent


def age_codes(db, email, seconds=3600):
    """Backdate a user's codes so the per-account cooldown allows another."""
    user = db.query(User).filter(User.email == email).first()
    for code in db.query(VerificationCode).filter(VerificationCode.user_id == user.id):
        code.created_at = code.created_at - timedelta(seconds=seconds)
    db.commit()


def login(client, email, password):
    return client.post("/api/v1/auth/login",
                       data={"username": email, "password": password})


# ── Registration and sign-in ─────────────────────────────────────────────────

def test_register_then_sign_in(client):
    seller = register(client)
    me = client.get("/api/v1/auth/me", headers=seller.headers)
    assert me.status_code == 200
    assert me.json()["email"] == seller.email


def test_short_passwords_are_refused(client):
    r = client.post("/api/v1/auth/register",
                    json={"email": "someone@example.com", "password": "short",
                          "full_name": "X"})
    assert r.status_code == 422


def test_duplicate_email_is_refused(client):
    seller = register(client)
    r = client.post("/api/v1/auth/register",
                    json={"email": seller.email, "password": PASSWORD, "full_name": "X"})
    assert r.status_code == 400


def test_wrong_password_is_refused(client):
    seller = register(client)
    assert login(client, seller.email, "wrong-password").status_code == 401


def test_logout_succeeds(client):
    assert client.post("/api/v1/auth/logout").status_code == 204


# ── Password reset ───────────────────────────────────────────────────────────

def test_reset_flow_replaces_the_password(client, mail):
    seller = register(client)
    assert client.post("/api/v1/auth/forgot-password",
                       json={"email": seller.email}).status_code == 202
    _, _, token = mail[0]

    r = client.post("/api/v1/auth/reset-password",
                    json={"token": token, "new_password": "newpass123"})

    assert r.status_code == 200
    assert login(client, seller.email, "newpass123").status_code == 200
    assert login(client, seller.email, PASSWORD).status_code == 401


def test_reset_token_works_only_once(client, mail):
    seller = register(client)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    _, _, token = mail[0]
    client.post("/api/v1/auth/reset-password",
                json={"token": token, "new_password": "newpass123"})

    r = client.post("/api/v1/auth/reset-password",
                    json={"token": token, "new_password": "otherpass123"})

    assert r.status_code == 400
    assert login(client, seller.email, "otherpass123").status_code == 401


def test_expired_reset_token_is_refused(client, mail, db):
    seller = register(client)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    _, _, token = mail[0]
    record = db.query(VerificationCode).filter(
        VerificationCode.purpose == verification.PASSWORD_RESET).first()
    record.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()

    r = client.post("/api/v1/auth/reset-password",
                    json={"token": token, "new_password": "newpass123"})

    assert r.status_code == 400


def test_issuing_a_new_token_retires_the_previous_one(client, mail, db):
    seller = register(client)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    age_codes(db, seller.email)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    superseded, current = mail[0][2], mail[1][2]

    assert client.post("/api/v1/auth/reset-password",
                       json={"token": superseded,
                             "new_password": "newpass123"}).status_code == 400
    assert client.post("/api/v1/auth/reset-password",
                       json={"token": current,
                             "new_password": "newpass123"}).status_code == 200


def test_garbage_token_is_refused(client):
    r = client.post("/api/v1/auth/reset-password",
                    json={"token": "not-a-token", "new_password": "newpass123"})
    assert r.status_code == 400


def test_reset_enforces_the_password_floor(client, mail):
    seller = register(client)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    r = client.post("/api/v1/auth/reset-password",
                    json={"token": mail[0][2], "new_password": "short"})
    assert r.status_code == 422


# ── One-time-code login ──────────────────────────────────────────────────────

def test_otp_flow_yields_a_usable_token(client, mail):
    seller = register(client)
    assert client.post("/api/v1/auth/otp/request",
                       json={"email": seller.email}).status_code == 202
    kind, _, code = mail[0]
    assert kind == "otp"
    assert len(code) == 6 and code.isdigit()

    r = client.post("/api/v1/auth/otp/verify", json={"email": seller.email, "code": code})

    assert r.status_code == 200
    token = r.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == seller.email


def test_otp_works_only_once(client, mail):
    seller = register(client)
    client.post("/api/v1/auth/otp/request", json={"email": seller.email})
    code = mail[0][2]
    client.post("/api/v1/auth/otp/verify", json={"email": seller.email, "code": code})

    r = client.post("/api/v1/auth/otp/verify", json={"email": seller.email, "code": code})

    assert r.status_code == 401


def test_guessing_is_capped_and_burns_the_code(client, mail):
    seller = register(client)
    client.post("/api/v1/auth/otp/request", json={"email": seller.email})
    real = mail[0][2]
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(settings.OTP_MAX_ATTEMPTS):
        assert client.post("/api/v1/auth/otp/verify",
                           json={"email": seller.email, "code": wrong}).status_code == 401

    r = client.post("/api/v1/auth/otp/verify", json={"email": seller.email, "code": real})
    assert r.status_code == 401, "the correct code must not work after the ceiling"


@pytest.mark.parametrize("code", ["12345", "1234567", ""])
def test_malformed_codes_are_refused(client, code):
    seller = register(client)
    r = client.post("/api/v1/auth/otp/verify", json={"email": seller.email, "code": code})
    assert r.status_code == 422


def test_resetting_a_password_kills_outstanding_otps(client, mail):
    """A code mailed before the reset must not still grant access after it."""
    seller = register(client)
    client.post("/api/v1/auth/otp/request", json={"email": seller.email})
    live_code = mail[0][2]
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    client.post("/api/v1/auth/reset-password",
                json={"token": mail[1][2], "new_password": "newpass123"})

    r = client.post("/api/v1/auth/otp/verify",
                    json={"email": seller.email, "code": live_code})

    assert r.status_code == 401


# ── Not leaking which addresses have accounts ────────────────────────────────

@pytest.mark.parametrize("endpoint", ["/api/v1/auth/forgot-password",
                                      "/api/v1/auth/otp/request"])
def test_unknown_address_is_answered_identically(client, mail, endpoint):
    seller = register(client)
    known = client.post(endpoint, json={"email": seller.email})
    mail.clear()
    unknown = client.post(endpoint, json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert mail == [], "nothing may be sent for an address with no account"


def test_verify_answers_the_same_for_wrong_code_and_unknown_address(client, mail):
    seller = register(client)
    client.post("/api/v1/auth/otp/request", json={"email": seller.email})

    wrong = client.post("/api/v1/auth/otp/verify",
                        json={"email": seller.email, "code": "000000"})
    unknown = client.post("/api/v1/auth/otp/verify",
                          json={"email": "nobody@example.com", "code": "000000"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


@pytest.mark.parametrize("endpoint", ["/api/v1/auth/forgot-password",
                                      "/api/v1/auth/otp/request"])
def test_disabled_accounts_are_sent_nothing(client, mail, db, endpoint):
    seller = register(client)
    user = db.query(User).filter(User.email == seller.email).first()
    user.is_active = False
    db.commit()

    r = client.post(endpoint, json={"email": seller.email})

    assert r.status_code == 202
    assert mail == []


@pytest.mark.parametrize("endpoint", ["/api/v1/auth/forgot-password",
                                      "/api/v1/auth/otp/request"])
def test_a_second_request_is_suppressed_but_looks_the_same(client, mail, endpoint):
    """Silently, so the cooldown cannot be used to probe for accounts either."""
    seller = register(client)
    first = client.post(endpoint, json={"email": seller.email})
    second = client.post(endpoint, json={"email": seller.email})

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(mail) == 1


# ── Storage of the codes themselves ──────────────────────────────────────────

def test_the_raw_code_is_never_stored(client, mail, db):
    seller = register(client)
    client.post("/api/v1/auth/forgot-password", json={"email": seller.email})
    token = mail[0][2]

    assert db.query(VerificationCode).filter(
        VerificationCode.code_hash == token).first() is None
    stored = db.query(VerificationCode).first()
    assert stored.code_hash != token


def test_hashing_is_keyed_so_a_dump_cannot_be_brute_forced(client):
    """Six digits is only 10^6 values — an unkeyed digest would be reversible."""
    assert verification.hash_code("123456") != hashlib.sha256(b"123456").hexdigest()
