"""Cross-endpoint pagination contract.

Every list that can truncate returns {items, total}. These tests pin the shape
for all of them at once, so a new list endpoint that forgets the envelope — or
an old one that loses it — fails here rather than silently shipping a list the
UI reads as "this is everything".
"""
from __future__ import annotations

import pytest

PAGINATED = [
    "/api/v1/products/",
    "/api/v1/customers/",
    "/api/v1/orders/",
    "/api/v1/conversations/",
]


@pytest.mark.parametrize("path", PAGINATED)
def test_list_returns_the_envelope(auth_client, path):
    client, _t, _u = auth_client
    body = client.get(path).json()
    assert set(body) == {"items", "total"}, f"{path} is not enveloped"
    assert isinstance(body["items"], list)
    assert body["total"] == 0


@pytest.mark.parametrize("path", PAGINATED)
def test_bounds_are_enforced_everywhere(auth_client, path):
    """A missing ceiling on any one endpoint is enough to let a caller ask the
    server to serialise a whole table, so this is checked per-endpoint."""
    client, _t, _u = auth_client
    assert client.get(path, params={"limit": 10_000}).status_code == 422
    assert client.get(path, params={"limit": 0}).status_code == 422
    assert client.get(path, params={"skip": -1}).status_code == 422


def test_integrations_stays_a_bare_array(auth_client):
    """Integrations is deliberately NOT enveloped: the query is unbounded, so
    there is no page to report. This pins that as a decision, not an oversight —
    if it ever gains a limit it must gain an envelope at the same time."""
    client, _t, _u = auth_client
    assert client.get("/api/v1/integrations/").json() == []


def test_customer_pages_walk_the_whole_list(auth_client):
    client, _t, _u = auth_client
    for i in range(12):
        assert client.post(
            "/api/v1/customers/", json={"name": f"C{i:02d}"}
        ).status_code == 201

    seen = []
    for skip in (0, 5, 10):
        page = client.get("/api/v1/customers/", params={"skip": skip, "limit": 5}).json()
        assert page["total"] == 12
        seen.extend(c["name"] for c in page["items"])

    assert sorted(seen) == [f"C{i:02d}" for i in range(12)]


def test_conversation_delete_is_gone(auth_client):
    """Removed on purpose: it CASCADEd every message and SET NULL'd the orders
    that referenced the thread. If someone reinstates it, this test should be
    the thing that makes them justify it."""
    client, _t, _u = auth_client
    cust = client.post("/api/v1/customers/", json={"name": "Dara"}).json()
    conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": cust["id"], "platform": "telegram"},
    ).json()

    assert client.delete(f"/api/v1/conversations/{conv['id']}").status_code == 405


def test_chat_simulator_endpoint_is_gone(auth_client):
    """The old POST /chat/{id} saved the caller's text as a *customer* message,
    so an authed seller could fabricate either side of a real transcript."""
    client, _t, _u = auth_client
    cust = client.post("/api/v1/customers/", json={"name": "Dara"}).json()
    conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": cust["id"], "platform": "telegram"},
    ).json()

    r = client.post(f"/api/v1/chat/{conv['id']}", json={"message": "hi"})
    assert r.status_code == 404
