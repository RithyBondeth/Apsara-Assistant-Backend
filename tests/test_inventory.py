"""Inventory V2: audited adjustments, reservations, and low-stock controls."""

from datetime import timedelta
from uuid import UUID

from app.core.clock import utcnow
from app.models.order import Order


def movements(client, seller, product_id=None):
    suffix = f"?product_id={product_id}" if product_id else ""
    response = client.get(f"/api/v1/inventory/movements{suffix}", headers=seller.headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_opening_stock_is_audited(client, seller):
    product = seller.product(stock=8)

    entries = movements(client, seller, product["id"])

    assert len(entries) == 1
    assert entries[0]["kind"] == "opening_balance"
    assert entries[0]["quantity_delta"] == 8
    assert entries[0]["balance_after"] == 8
    assert entries[0]["created_by_user_id"] is not None


def test_manual_adjustment_requires_reason_and_records_balance(client, seller):
    product = seller.product(stock=8)

    response = client.post(
        f"/api/v1/inventory/products/{product['id']}/adjustments",
        json={"quantity_delta": 5, "reason": "Supplier delivery"},
        headers=seller.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["stock"] == 13
    entry = movements(client, seller, product["id"])[0]
    assert entry["kind"] == "manual_adjustment"
    assert entry["quantity_delta"] == 5
    assert entry["reason"] == "Supplier delivery"


def test_adjustment_cannot_make_available_stock_negative(client, seller):
    product = seller.product(stock=2)

    response = client.post(
        f"/api/v1/inventory/products/{product['id']}/adjustments",
        json={"quantity_delta": -3, "reason": "Damaged goods"},
        headers=seller.headers,
    )

    assert response.status_code == 400
    assert seller.stock_of(product["id"]) == 2
    assert len(movements(client, seller, product["id"])) == 1


def test_inventory_is_tenant_scoped(client, seller, other_seller):
    product = seller.product(stock=4)

    response = client.post(
        f"/api/v1/inventory/products/{product['id']}/adjustments",
        json={"quantity_delta": 1, "reason": "Not my stock"},
        headers=other_seller.headers,
    )

    assert response.status_code == 404
    assert movements(client, other_seller) == []


def test_product_deletion_preserves_inventory_history(client, seller):
    product = seller.product(stock=4)

    response = client.delete(f"/api/v1/products/{product['id']}", headers=seller.headers)

    assert response.status_code == 204, response.text
    entry = movements(client, seller)[0]
    assert entry["product_id"] is None
    assert entry["product_name"] == product["name"]


def test_order_reserves_then_fulfils_stock(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()

    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}])

    assert order.status_code == 201, order.text
    assert order.json()["reservation_expires_at"] is not None
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (5, 3)

    fulfilled = client.patch(
        f"/api/v1/orders/{order.json()['id']}",
        json={"status": "shipped"},
        headers=seller.headers,
    )
    assert fulfilled.status_code == 200, fulfilled.text
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (5, 0)
    assert movements(client, seller, product["id"])[0]["kind"] == "reservation_fulfilled"


def test_duplicate_order_lines_share_one_reservation(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()

    response = seller.order(
        customer["id"],
        [
            {"product_id": product["id"], "quantity": 2},
            {"product_id": product["id"], "quantity": 3},
        ],
    )

    assert response.status_code == 201, response.text
    assert [(item["quantity"], item["subtotal"]) for item in response.json()["items"]] == [
        (5, "62.50")
    ]
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (3, 5)


def test_backward_status_transition_restores_reservation(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}]).json()
    client.patch(
        f"/api/v1/orders/{order['id']}", json={"status": "shipped"}, headers=seller.headers
    )

    response = client.patch(
        f"/api/v1/orders/{order['id']}", json={"status": "processing"}, headers=seller.headers
    )

    assert response.status_code == 200, response.text
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (5, 3)


def test_cancellation_releases_reserved_stock(client, seller):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}]).json()

    response = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"status": "cancelled"},
        headers=seller.headers,
    )

    assert response.status_code == 200, response.text
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (8, 0)


def test_expired_pending_reservations_are_released(client, seller, db):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}]).json()
    stored = db.query(Order).filter(Order.id == UUID(order["id"])).one()
    stored.reservation_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    response = client.post("/api/v1/inventory/release-expired", headers=seller.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"released_orders": 1, "released_units": 3}
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (8, 0)
    saved_order = client.get(f"/api/v1/orders/{order['id']}", headers=seller.headers).json()
    assert saved_order["status"] == "cancelled"


def test_order_awaiting_payment_never_expires_its_stock(client, seller, db):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}]).json()
    stored = db.query(Order).filter(Order.id == UUID(order["id"])).one()
    stored.payment_status = "pending"
    stored.reservation_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    response = client.post("/api/v1/inventory/release-expired", headers=seller.headers)

    assert response.json() == {"released_orders": 0, "released_units": 0}
    current = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert (current["stock"], current["reserved_stock"]) == (5, 3)


def test_low_stock_threshold_is_configurable(client, seller):
    product = seller.product(stock=8, low_stock_threshold=3)
    assert product["low_stock_threshold"] == 3

    response = client.patch(
        f"/api/v1/products/{product['id']}",
        json={"low_stock_threshold": 10},
        headers=seller.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["low_stock_threshold"] == 10
