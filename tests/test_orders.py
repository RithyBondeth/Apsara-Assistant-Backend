"""Order management, and the stock movements it drives."""

import uuid


def test_creating_an_order_prices_it_server_side(client, seller):
    product = seller.product(price="12.50", stock=8)
    customer = seller.customer()

    r = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}])

    assert r.status_code == 201, r.text
    order = r.json()
    assert order["total_amount"] == "37.50"
    assert order["items"][0]["unit_price"] == "12.50"
    assert order["items"][0]["subtotal"] == "37.50"
    assert order["status"] == "pending"


def test_client_cannot_dictate_the_price(client, seller):
    """unit_price is not part of the create schema; a smuggled one is ignored."""
    product = seller.product(price="12.50", stock=8)
    customer = seller.customer()

    r = seller.order(customer["id"],
                     [{"product_id": product["id"], "quantity": 1, "unit_price": "0.01"}])

    assert r.status_code == 201
    assert r.json()["items"][0]["unit_price"] == "12.50"


def test_creating_an_order_deducts_stock(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()

    seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}])

    assert seller.stock_of(product["id"]) == 5


def test_order_beyond_stock_is_refused(client, seller):
    product = seller.product(stock=2)
    customer = seller.customer()

    r = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}])

    assert r.status_code == 400
    assert "Insufficient stock" in r.json()["detail"]
    assert seller.stock_of(product["id"]) == 2


def test_empty_order_is_refused(client, seller):
    customer = seller.customer()
    assert seller.order(customer["id"], []).status_code == 400


def test_inactive_product_cannot_be_ordered(client, seller):
    product = seller.product(stock=5)
    client.patch(f"/api/v1/products/{product['id']}", json={"is_active": False},
                 headers=seller.headers)
    customer = seller.customer()

    r = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])

    assert r.status_code == 400
    assert "not available" in r.json()["detail"]


# ── The cancelled/active boundary ────────────────────────────────────────────

def test_cancelling_returns_stock(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()

    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                 headers=seller.headers)

    assert seller.stock_of(product["id"]) == 8


def test_reviving_a_cancelled_order_takes_the_stock_back(client, seller):
    """Cancelling restocked but reviving did not re-deduct, so each
    cancel/revive cycle minted inventory the seller never had."""
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                 headers=seller.headers)
    assert seller.stock_of(product["id"]) == 8

    r = client.patch(f"/api/v1/orders/{order['id']}", json={"status": "confirmed"},
                     headers=seller.headers)

    assert r.status_code == 200
    assert seller.stock_of(product["id"]) == 5


def test_cancelling_twice_does_not_restock_twice(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()

    for _ in range(2):
        client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                     headers=seller.headers)

    assert seller.stock_of(product["id"]) == 8


def test_transitions_between_active_states_leave_stock_alone(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()

    for status in ["confirmed", "processing", "shipped", "delivered"]:
        client.patch(f"/api/v1/orders/{order['id']}", json={"status": status},
                     headers=seller.headers)
        assert seller.stock_of(product["id"]) == 5


def test_reviving_is_refused_when_the_stock_has_since_sold(client, seller):
    product = seller.product(stock=2)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 2}]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                 headers=seller.headers)
    seller.order(customer["id"], [{"product_id": product["id"], "quantity": 2}])

    r = client.patch(f"/api/v1/orders/{order['id']}", json={"status": "confirmed"},
                     headers=seller.headers)

    assert r.status_code == 400
    assert "insufficient stock" in r.json()["detail"].lower()
    assert client.get(f"/api/v1/orders/{order['id']}",
                      headers=seller.headers).json()["status"] == "cancelled"
    assert seller.stock_of(product["id"]) == 0


def test_deleting_an_active_order_restocks(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()

    assert client.delete(f"/api/v1/orders/{order['id']}",
                         headers=seller.headers).status_code == 204
    assert seller.stock_of(product["id"]) == 8


def test_deleting_a_cancelled_order_does_not_restock_again(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 3}]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                 headers=seller.headers)

    client.delete(f"/api/v1/orders/{order['id']}", headers=seller.headers)

    assert seller.stock_of(product["id"]) == 8


def test_unknown_status_is_refused(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 1}]).json()

    r = client.patch(f"/api/v1/orders/{order['id']}", json={"status": "banana"},
                     headers=seller.headers)

    assert r.status_code == 400
    assert "Invalid status" in r.json()["detail"]


# ── Tenancy ──────────────────────────────────────────────────────────────────

def test_cannot_order_another_sellers_product(client, seller, other_seller):
    product = seller.product(stock=8)
    customer = other_seller.customer()

    r = other_seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])

    assert r.status_code == 404
    assert seller.stock_of(product["id"]) == 8


def test_cannot_order_for_another_sellers_customer(client, seller, other_seller):
    product = other_seller.product(stock=8)
    customer = seller.customer()

    r = other_seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])

    assert r.status_code == 404


def test_orders_are_listed_per_seller(client, seller, other_seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])

    assert len(client.get("/api/v1/orders/", headers=seller.headers).json()) == 1
    assert client.get("/api/v1/orders/", headers=other_seller.headers).json() == []


def test_another_seller_cannot_read_or_change_an_order(client, seller, other_seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 1}]).json()

    assert client.get(f"/api/v1/orders/{order['id']}",
                      headers=other_seller.headers).status_code == 404
    assert client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                        headers=other_seller.headers).status_code == 404
    assert client.delete(f"/api/v1/orders/{order['id']}",
                         headers=other_seller.headers).status_code == 404


def test_unknown_order_is_404(client, seller):
    assert client.get(f"/api/v1/orders/{uuid.uuid4()}",
                      headers=seller.headers).status_code == 404


def test_filtering_by_status(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    first = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 1}]).json()
    seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])
    client.patch(f"/api/v1/orders/{first['id']}", json={"status": "cancelled"},
                 headers=seller.headers)

    cancelled = client.get("/api/v1/orders/?status=cancelled", headers=seller.headers).json()

    assert [o["id"] for o in cancelled] == [first["id"]]
