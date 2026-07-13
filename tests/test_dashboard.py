"""Dashboard stats aggregation tests."""
from __future__ import annotations


def test_stats_empty(auth_client):
    client, _token, _uid = auth_client
    r = client.get("/api/v1/dashboard/stats")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "products": 0,
        "customers": 0,
        "conversations": 0,
        "open_conversations": 0,
        "orders": 0,
        "pending_orders": 0,
        "revenue": "0",
    }


def test_stats_counts_and_revenue(auth_client):
    client, _token, _uid = auth_client
    customer_id = client.post("/api/v1/customers/", json={"name": "Dara"}).json()["id"]
    product_id = client.post(
        "/api/v1/products/", json={"name": "Hat", "price": "20.00", "stock": 10}
    ).json()["id"]
    client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 2}]},
    )

    body = client.get("/api/v1/dashboard/stats").json()
    assert body["products"] == 1
    assert body["customers"] == 1
    assert body["orders"] == 1
    assert body["pending_orders"] == 1
    assert body["revenue"] == "40.00"


def test_stats_requires_auth(client):
    assert client.get("/api/v1/dashboard/stats").status_code == 401
