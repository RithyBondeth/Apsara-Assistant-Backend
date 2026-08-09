"""Shared fixtures.

Tests run against a real Postgres database rather than SQLite: the models use
the postgresql UUID dialect and `with_for_update()`, neither of which SQLite
supports, so an in-memory substitute would not exercise the code that ships.

The database is named by TEST_DATABASE_URL, defaulting to a local `apsara_test_db`.
It is created from the models at the start of the session and truncated between
tests. Point it at a throwaway database — it is wiped repeatedly.
"""

import os
import uuid

# Must precede any app import: app.database builds its engine from this at
# import time, and app.core.config reads it into Settings.
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql://localhost:5432/apsara_test_db",
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.setdefault("SECRET_KEY", "test-secret-not-used-outside-tests")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

from app.core import security  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "secret123"

# bcrypt is deliberately slow, and almost every test registers a seller. At the
# production cost factor that alone accounted for most of the suite's runtime;
# the algorithm under test is unchanged, only the work factor.
security.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto",
                                    bcrypt__rounds=4)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean():
    """Empty every table between tests so ordering cannot matter."""
    yield
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    """A session for asserting on or manipulating state directly."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ── Actors and fixtures data ─────────────────────────────────────────────────

class Seller:
    """A registered seller plus helpers to give them things to sell."""

    def __init__(self, client, email, headers):
        self.client = client
        self.email = email
        self.headers = headers

    def product(self, name="Silk Scarf", price="12.50", stock=8, **kw):
        r = self.client.post("/api/v1/products/",
                             json={"name": name, "price": price, "stock": stock, **kw},
                             headers=self.headers)
        assert r.status_code == 201, r.text
        return r.json()

    def customer(self, name="Srey Neang", platform="messenger", **kw):
        r = self.client.post("/api/v1/customers/",
                             json={"name": name, "platform": platform,
                                   "platform_id": uuid.uuid4().hex, **kw},
                             headers=self.headers)
        assert r.status_code == 201, r.text
        return r.json()

    def conversation(self, customer_id=None, platform="messenger"):
        customer_id = customer_id or self.customer(platform=platform)["id"]
        r = self.client.post("/api/v1/conversations/",
                             json={"customer_id": customer_id, "platform": platform},
                             headers=self.headers)
        assert r.status_code == 201, r.text
        return r.json()

    def order(self, customer_id, items, **kw):
        return self.client.post("/api/v1/orders/",
                                json={"customer_id": customer_id, "items": items, **kw},
                                headers=self.headers)

    def stock_of(self, product_id):
        return self.client.get(f"/api/v1/products/{product_id}",
                               headers=self.headers).json()["stock"]


def register(client, business_name="Sok Silk Shop", password=PASSWORD):
    email = f"{uuid.uuid4().hex[:12]}@example.com"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": password,
                          "full_name": "Sok Dara", "business_name": business_name})
    assert r.status_code == 201, r.text
    token = client.post("/api/v1/auth/login",
                        data={"username": email, "password": password})
    assert token.status_code == 200, token.text
    return Seller(client, email, {"Authorization": f"Bearer {token.json()['access_token']}"})


@pytest.fixture
def seller(client):
    return register(client)


@pytest.fixture
def other_seller(client):
    """A second tenant, for proving one seller cannot reach another's data."""
    return register(client, business_name="Rival Shop")
