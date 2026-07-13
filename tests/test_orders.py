"""Order tests: server-side pricing, stock decrement, restock-on-cancel."""
from __future__ import annotations

import pytest


@pytest.fixture()
def shop(auth_client):
    """A seller with one customer and one product (price 10.00, stock 5)."""
    client, _token, _uid = auth_client
    customer_id = client.post(
        "/api/v1/customers/", json={"name": "Dara"}
    ).json()["id"]
    product_id = client.post(
        "/api/v1/products/", json={"name": "Scarf", "price": "10.00", "stock": 5}
    ).json()["id"]
    return client, customer_id, product_id


def test_create_order_prices_and_decrements_stock(shop):
    client, customer_id, product_id = shop

    r = client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 3}]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # 3 × 10.00, priced server-side
    assert body["total_amount"] == "30.00"
    assert body["items"][0]["unit_price"] == "10.00"
    assert body["items"][0]["subtotal"] == "30.00"
    assert body["status"] == "pending"

    # stock went 5 → 2
    assert client.get(f"/api/v1/products/{product_id}").json()["stock"] == 2


def test_create_order_ignores_client_supplied_price(shop):
    client, customer_id, product_id = shop
    # even if a client sneaks in unit_price, the server uses the product price
    r = client.post(
        "/api/v1/orders/",
        json={
            "customer_id": customer_id,
            "items": [{"product_id": product_id, "quantity": 1, "unit_price": "0.01"}],
        },
    )
    assert r.status_code == 201
    assert r.json()["items"][0]["unit_price"] == "10.00"


def test_insufficient_stock_rejected(shop):
    client, customer_id, product_id = shop
    r = client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 99}]},
    )
    assert r.status_code == 400
    # stock untouched
    assert client.get(f"/api/v1/products/{product_id}").json()["stock"] == 5


def test_cancel_order_restocks(shop):
    client, customer_id, product_id = shop
    order_id = client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 4}]},
    ).json()["id"]
    assert client.get(f"/api/v1/products/{product_id}").json()["stock"] == 1

    r = client.patch(f"/api/v1/orders/{order_id}", json={"status": "cancelled"})
    assert r.status_code == 200
    # stock restored 1 → 5
    assert client.get(f"/api/v1/products/{product_id}").json()["stock"] == 5


def test_same_product_across_items_aggregates_stock(shop):
    client, customer_id, product_id = shop  # stock = 5
    # Two line items for the same product: 3 + 3 = 6 > 5 available
    r = client.post(
        "/api/v1/orders/",
        json={
            "customer_id": customer_id,
            "items": [
                {"product_id": product_id, "quantity": 3},
                {"product_id": product_id, "quantity": 3},
            ],
        },
    )
    assert r.status_code == 400
    # nothing committed — stock untouched
    assert client.get(f"/api/v1/products/{product_id}").json()["stock"] == 5


def test_invalid_status_rejected(shop):
    client, customer_id, product_id = shop
    order_id = client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 1}]},
    ).json()["id"]
    r = client.patch(f"/api/v1/orders/{order_id}", json={"status": "teleported"})
    assert r.status_code == 400
