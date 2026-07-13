"""Shared pytest fixtures.

Runs the full API against an in-memory SQLite database so the suite needs no
external services. The one incompatibility — PostgreSQL's UUID column type — is
bridged with a compile hook that renders it as CHAR(36) on SQLite.
"""
from __future__ import annotations

import os

# Configure the app for testing BEFORE importing anything that reads settings.
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "CHAR(36)"


import app.models  # noqa: E402,F401  (register all models on Base.metadata)
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

# One in-memory DB shared across the test's connections via StaticPool.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db_session():
    """Fresh schema per test for full isolation."""
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    """TestClient whose get_db yields the test session."""

    def _override_get_db():
        # Mirror production: each request gets the session and any work left
        # uncommitted (e.g. a request that raised mid-transaction) is rolled
        # back when the session closes, rather than leaking to the next request.
        try:
            yield db_session
        finally:
            db_session.rollback()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(client):
    """A client with a registered+logged-in user; returns (client, token, user_id)."""
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller@example.com",
            "password": "supersecret",
            "full_name": "Test Seller",
            "business_name": "Test Shop",
        },
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]

    login = client.post(
        "/api/v1/auth/login",
        data={"username": "seller@example.com", "password": "supersecret"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client, token, user_id
