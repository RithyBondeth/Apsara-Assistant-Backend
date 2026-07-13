"""Auth endpoint tests."""
from __future__ import annotations


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
