from app.models.inventory_movement import InventoryMovement
from app.models.operations import LowStockAlert
from app.models.platform_connection import PlatformConnection
from app.models.user import User
from app.core.crypto import encrypt


def supplier(client, seller, name="Khmer Textiles"):
    response = client.post("/api/v1/operations/suppliers", json={"name": name}, headers=seller.headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_low_stock_episode_opens_once_and_resolves_after_receiving(client, seller, db):
    product = seller.product(stock=2, low_stock_threshold=5)
    first = client.get("/api/v1/operations/alerts", headers=seller.headers).json()
    second = client.get("/api/v1/operations/alerts", headers=seller.headers).json()
    assert len(first) == len(second) == 1
    assert first[0]["stock"] == 2

    source = supplier(client, seller)
    po = client.post("/api/v1/operations/purchase-orders", json={
        "supplier_id": source["id"],
        "items": [{"variant_id": product["variants"][0]["id"], "ordered_quantity": 10, "unit_cost": "4.25"}],
    }, headers=seller.headers).json()
    client.patch(f"/api/v1/operations/purchase-orders/{po['id']}", json={"status": "ordered"}, headers=seller.headers)
    received = client.post(f"/api/v1/operations/purchase-orders/{po['id']}/receive", json={
        "items": [{"item_id": po["items"][0]["id"], "quantity": 10}]
    }, headers=seller.headers)
    assert received.status_code == 200, received.text
    assert received.json()["status"] == "received"
    assert seller.stock_of(product["id"]) == 12
    assert client.get("/api/v1/operations/alerts", headers=seller.headers).json() == []
    assert db.query(InventoryMovement).filter(InventoryMovement.kind == "purchase_received").one().purchase_order_id


def test_purchase_order_partial_receiving_is_bounded_and_tenant_scoped(client, seller, other_seller):
    product = seller.product(stock=8)
    source = supplier(client, seller)
    po = client.post("/api/v1/operations/purchase-orders", json={
        "supplier_id": source["id"],
        "items": [{"variant_id": product["variants"][0]["id"], "ordered_quantity": 5, "unit_cost": "3.00"}],
    }, headers=seller.headers).json()
    client.patch(f"/api/v1/operations/purchase-orders/{po['id']}", json={"status": "ordered"}, headers=seller.headers)
    denied = client.post(f"/api/v1/operations/purchase-orders/{po['id']}/receive", json={
        "items": [{"item_id": po["items"][0]["id"], "quantity": 2}]
    }, headers=other_seller.headers)
    assert denied.status_code == 404
    partial = client.post(f"/api/v1/operations/purchase-orders/{po['id']}/receive", json={
        "items": [{"item_id": po["items"][0]["id"], "quantity": 2}]
    }, headers=seller.headers)
    assert partial.json()["status"] == "partially_received"
    too_many = client.post(f"/api/v1/operations/purchase-orders/{po['id']}/receive", json={
        "items": [{"item_id": po["items"][0]["id"], "quantity": 4}]
    }, headers=seller.headers)
    assert too_many.status_code == 400
    assert seller.stock_of(product["id"]) == 10


def test_return_refund_and_inventory_restoration_are_idempotent(client, seller, db):
    product = seller.product(stock=8)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 3}]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "delivered"}, headers=seller.headers)
    created = client.post("/api/v1/operations/returns", json={
        "order_id": order["id"], "reason": "Customer changed their mind", "refund_amount": "25.00",
        "items": [{"order_item_id": order["items"][0]["id"], "quantity": 2, "restock_quantity": 1}],
    }, headers=seller.headers)
    assert created.status_code == 201, created.text
    value = created.json()
    received = client.post(f"/api/v1/operations/returns/{value['id']}/receive", headers=seller.headers)
    assert received.status_code == 200 and seller.stock_of(product["id"]) == 6
    assert client.post(f"/api/v1/operations/returns/{value['id']}/receive", headers=seller.headers).status_code == 409
    refunded = client.post(f"/api/v1/operations/returns/{value['id']}/refund", headers=seller.headers)
    assert refunded.status_code == 200 and refunded.json()["status"] == "refunded"
    assert db.query(InventoryMovement).filter(InventoryMovement.kind == "return_received").one().sales_return_id


def test_reports_show_best_sellers_and_reorder_forecast(client, seller):
    product = seller.product(name="Krama", stock=20, low_stock_threshold=5)
    customer = seller.customer()
    order = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 6}]).json()
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "shipped"}, headers=seller.headers)
    report = client.get("/api/v1/operations/reports?days=30&forecast_days=90", headers=seller.headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert (body["total_orders"], body["total_units"], body["total_revenue"]) == (1, 6, "75.00")
    assert body["best_sellers"][0]["product_name"] == "Krama"
    assert body["forecast"][0]["forecast_units"] == 18
    assert body["forecast"][0]["suggested_reorder"] == 9


def test_low_stock_delivery_uses_email_and_connected_telegram(client, seller, db, monkeypatch):
    user = db.query(User).filter(User.email == seller.email).one()
    user.low_stock_telegram_enabled = True
    user.low_stock_telegram_chat_id = "778899"
    db.add(PlatformConnection(user_id=user.id, platform="telegram", external_id="alert-bot",
                              access_token=encrypt("bot-token"), is_active=True))
    db.commit()
    sent = []
    monkeypatch.setattr("app.services.alerts.send_email", lambda to, subject, body: sent.append(("email", to, body)) or True)
    monkeypatch.setattr("app.services.alerts.send_reply", lambda platform, token, chat, body: sent.append((platform, chat, body)) or True)

    seller.product(stock=1, low_stock_threshold=2)

    assert [(entry[0], entry[1]) for entry in sent] == [("email", seller.email), ("telegram", "778899")]
