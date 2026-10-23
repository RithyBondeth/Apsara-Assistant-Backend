"""Signing up: the address must be confirmed, and bulk sign-ups are capped."""

from datetime import timedelta
from unittest import mock

import pytest

from app.api.v1.endpoints import auth
from app.core.config import settings
from app.models.user import User
from app.models.verification_code import VerificationCode
from tests.conftest import PASSWORD


def sign_up(client, email="new@example.com", headers=None):
    with mock.patch.object(auth, "send_email_verification") as mail:
        r = client.post("/api/v1/auth/register", headers=headers or {},
                        json={"email": email, "password": PASSWORD, "full_name": "Sok"})
    return r, (mail.call_args.args[1] if mail.called else None)


def sign_in(client, email="new@example.com"):
    return client.post("/api/v1/auth/login", data={"username": email, "password": PASSWORD})


def test_a_new_account_is_emailed_a_link_and_waits_for_it(client):
    r, token = sign_up(client)
    assert r.status_code == 201
    assert token
    assert r.json()["email_verified_at"] is None

    refused = sign_in(client)
    assert refused.status_code == 403
    assert refused.json()["detail"].startswith("Verify your email")

    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    assert sign_in(client).status_code == 200


def test_the_link_works_once(client):
    _, token = sign_up(client)
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400
    assert client.post("/api/v1/auth/verify-email", json={"token": "nonsense"}).status_code == 400


def test_an_expired_link_is_refused(client, db):
    _, token = sign_up(client)
    db.query(VerificationCode).update(
        {VerificationCode.expires_at: VerificationCode.expires_at - timedelta(days=3)})
    db.commit()
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400


def test_resending_says_the_same_thing_whoever_asks(client, db):
    sign_up(client)
    db.query(VerificationCode).update(
        {VerificationCode.created_at: VerificationCode.created_at - timedelta(minutes=5)})
    db.commit()

    with mock.patch.object(auth, "send_email_verification") as mail:
        unknown = client.post("/api/v1/auth/verify-email/resend", json={"email": "who@example.com"})
        known = client.post("/api/v1/auth/verify-email/resend", json={"email": "new@example.com"})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert mail.call_count == 1
    assert client.post("/api/v1/auth/verify-email",
                       json={"token": mail.call_args.args[1]}).status_code == 200


def test_a_confirmed_address_is_not_sent_another_link(client, seller):
    with mock.patch.object(auth, "send_email_verification") as mail:
        client.post("/api/v1/auth/verify-email/resend", json={"email": seller.email})
    mail.assert_not_called()


def test_a_password_reset_also_confirms_the_address(client, db):
    sign_up(client)
    db.query(VerificationCode).update(
        {VerificationCode.created_at: VerificationCode.created_at - timedelta(minutes=5)})
    db.commit()
    with mock.patch.object(auth, "send_password_reset") as mail:
        client.post("/api/v1/auth/forgot-password", json={"email": "new@example.com"})
    client.post("/api/v1/auth/reset-password",
                json={"token": mail.call_args.args[1], "new_password": PASSWORD})
    assert sign_in(client).status_code == 200


def test_signing_in_with_a_code_also_confirms_the_address(client, db):
    sign_up(client)
    with mock.patch.object(auth, "send_login_otp") as mail:
        client.post("/api/v1/auth/otp/request", json={"email": "new@example.com"})
    r = client.post("/api/v1/auth/otp/verify",
                    json={"email": "new@example.com", "code": mail.call_args.args[1]})
    assert r.status_code == 200
    db.expire_all()
    assert db.query(User).one().email_verified_at is not None


# ── Throttling ───────────────────────────────────────────────────────────────

@pytest.fixture
def capped(monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_MAX_PER_IP", 2)
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "X-Real-IP")


def test_one_address_can_only_create_so_many_accounts(client, capped):
    office = {"X-Real-IP": "203.0.113.7"}
    assert sign_up(client, "a@example.com", office)[0].status_code == 201
    assert sign_up(client, "b@example.com", office)[0].status_code == 201

    third, token = sign_up(client, "c@example.com", office)
    assert third.status_code == 429
    assert token is None, "no email for a refused sign-up"
    assert "Retry-After" in third.headers

    elsewhere = sign_up(client, "c@example.com", {"X-Real-IP": "198.51.100.4"})[0]
    assert elsewhere.status_code == 201


def test_the_cap_forgets_old_sign_ups(client, capped, db):
    from app.models.signup_attempt import SignupAttempt

    office = {"X-Real-IP": "203.0.113.7"}
    sign_up(client, "a@example.com", office)
    sign_up(client, "b@example.com", office)
    db.query(SignupAttempt).update(
        {SignupAttempt.created_at: SignupAttempt.created_at - timedelta(hours=2)})
    db.commit()

    assert sign_up(client, "c@example.com", office)[0].status_code == 201
