"""AI order proposals are schema-bound, tenant-scoped, and never auto-applied."""

from uuid import UUID, uuid4

from app.api.v1.endpoints import chat
from app.models.message import Message
from app.models.order import Order
from app.services.ai_service import ExtractedOrderDraft, ExtractedOrderItem


def conversation_with_text(seller, db, text="I want two scarves at 12 Main St"):
    customer = seller.customer()
    conversation = seller.conversation(customer["id"])
    db.add(Message(
        conversation_id=conversation["id"], sender_type="customer",
        message_type="text", content=text,
    ))
    db.commit()
    return customer, conversation


def extracted(product_id, quantity=2, address="12 Main St", notes=None):
    return ExtractedOrderDraft(
        items=[ExtractedOrderItem(product_id=product_id, quantity=quantity)],
        delivery_address=address,
        notes=notes,
    )


def test_draft_maps_catalogue_products_without_creating_an_order(
    client, seller, db, monkeypatch
):
    product = seller.product(name="Silk Scarf", price="12.50", stock=8)
    customer, conversation = conversation_with_text(seller, db)
    monkeypatch.setattr(chat, "generate_order_draft",
                        lambda messages: extracted(product["id"], notes="Blue if available"))

    response = client.post(f"/api/v1/chat/{conversation['id']}/order-draft",
                           headers=seller.headers)

    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["customer_id"] == customer["id"]
    assert draft["delivery_address"] == "12 Main St"
    assert draft["notes"] == "Blue if available"
    assert draft["items"] == [{
        "product_id": product["id"], "product_name": "Silk Scarf",
        "variant_id": product["variants"][0]["id"], "variant_name": "Default",
        "variant_options": {},
        "quantity": 2, "unit_price": "12.50", "subtotal": "25.00", "stock": 8,
    }]
    assert db.query(Order).count() == 0
    assert seller.stock_of(product["id"]) == 8


def test_unknown_product_ids_are_removed(client, seller, db, monkeypatch):
    seller.product()
    _, conversation = conversation_with_text(seller, db)
    monkeypatch.setattr(chat, "generate_order_draft",
                        lambda messages: extracted(uuid4()))

    draft = client.post(f"/api/v1/chat/{conversation['id']}/order-draft",
                        headers=seller.headers).json()

    assert draft["items"] == []
    assert "items" in draft["missing_fields"]
    assert "removed" in draft["warnings"][0]


def test_duplicate_lines_are_combined_and_stock_shortage_is_flagged(
    client, seller, db, monkeypatch
):
    product = seller.product(stock=2)
    _, conversation = conversation_with_text(seller, db)
    monkeypatch.setattr(chat, "generate_order_draft", lambda messages: ExtractedOrderDraft(
        items=[
            ExtractedOrderItem(product_id=UUID(product["id"]), quantity=2),
            ExtractedOrderItem(product_id=UUID(product["id"]), quantity=1),
        ],
        delivery_address=None,
        notes=None,
    ))

    draft = client.post(f"/api/v1/chat/{conversation['id']}/order-draft",
                        headers=seller.headers).json()

    assert draft["items"][0]["quantity"] == 3
    assert "only 2" in draft["warnings"][0]
    assert "delivery address" in draft["missing_fields"]


def test_draft_is_tenant_scoped(client, seller, other_seller, db, monkeypatch):
    seller.product()
    _, conversation = conversation_with_text(seller, db)
    monkeypatch.setattr(chat, "generate_order_draft", lambda messages: extracted(uuid4()))

    response = client.post(f"/api/v1/chat/{conversation['id']}/order-draft",
                           headers=other_seller.headers)

    assert response.status_code == 404


def test_draft_needs_text_and_an_active_product(client, seller, db):
    empty = seller.conversation()
    no_text = client.post(f"/api/v1/chat/{empty['id']}/order-draft",
                          headers=seller.headers)
    assert no_text.status_code == 400
    db.add(Message(conversation_id=empty["id"], sender_type="customer",
                   message_type="text", content="one please"))
    db.commit()
    no_product = client.post(f"/api/v1/chat/{empty['id']}/order-draft",
                             headers=seller.headers)
    assert no_product.status_code == 400
