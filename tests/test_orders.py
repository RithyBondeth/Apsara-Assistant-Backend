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


def test_order_links_to_a_conversation_and_is_listable_by_it(shop):
    """The chat panel needs to find the orders that came out of a thread."""
    client, customer_id, product_id = shop
    conv_id = client.post(
        "/api/v1/conversations/",
        json={"customer_id": customer_id, "platform": "telegram"},
    ).json()["id"]

    r = client.post(
        "/api/v1/orders/",
        json={
            "customer_id": customer_id,
            "conversation_id": conv_id,
            "items": [{"product_id": product_id, "quantity": 1}],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["conversation_id"] == conv_id

    # A second order NOT from this thread must not show up in its list.
    client.post(
        "/api/v1/orders/",
        json={"customer_id": customer_id, "items": [{"product_id": product_id, "quantity": 1}]},
    )

    listed = client.get(f"/api/v1/orders/?conversation_id={conv_id}").json()["items"]
    assert len(listed) == 1
    assert listed[0]["conversation_id"] == conv_id


def test_order_rejects_a_conversation_owned_by_someone_else(shop):
    """conversation_id must belong to the seller — no linking to a stranger's
    thread. The shared TestClient means we build the stranger's thread first,
    then restore the owner's auth so the customer check passes and only the
    conversation ownership is what can fail."""
    client, customer_id, product_id = shop
    owner_auth = dict(client.headers)

    # A second seller with their own conversation.
    client.post(
        "/api/v1/auth/register",
        json={"email": "other@shop.com", "password": "password123", "full_name": "Other"},
    )
    token = client.post(
        "/api/v1/auth/login", data={"username": "other@shop.com", "password": "password123"}
    ).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    other_customer = client.post("/api/v1/customers/", json={"name": "X"}).json()["id"]
    stranger_conv = client.post(
        "/api/v1/conversations/",
        json={"customer_id": other_customer, "platform": "telegram"},
    ).json()["id"]

    # Back to the owner: their own customer, but the stranger's conversation.
    client.headers.update(owner_auth)
    r = client.post(
        "/api/v1/orders/",
        json={
            "customer_id": customer_id,
            "conversation_id": stranger_conv,
            "items": [{"product_id": product_id, "quantity": 1}],
        },
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "conversation_not_found"
