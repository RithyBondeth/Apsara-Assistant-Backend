"""Variant-level catalogue, inventory, order, image, and tenancy behavior."""

from app.services.ai_service import build_system_prompt
from app.models.user import User

PNG = b"\x89PNG\r\n\x1a\n" + b"variant-image"


def create_variant(client, seller, product_id, *, options, price="14.00", stock=4, **extra):
    return client.post(
        f"/api/v1/products/{product_id}/variants",
        json={
            "option_values": options,
            "price": price,
            "stock": stock,
            "low_stock_threshold": 2,
            **extra,
        },
        headers=seller.headers,
    )


def test_existing_shape_creates_default_variant_and_aggregate(client, seller):
    product = seller.product(price="12.50", stock=8, low_stock_threshold=3)

    assert len(product["variants"]) == 1
    variant = product["variants"][0]
    assert variant["name"] == "Default"
    assert variant["option_values"] == {}
    assert (variant["price"], variant["stock"], variant["low_stock_threshold"]) == (
        "12.50", 8, 3,
    )
    movement = client.get(
        "/api/v1/inventory/movements", headers=seller.headers
    ).json()[0]
    assert movement["variant_id"] == variant["id"]
    assert movement["variant_name"] == "Default"


def test_custom_variants_have_separate_price_stock_sku_and_barcode(client, seller):
    response = client.post(
        "/api/v1/products/",
        json={
            "name": "T-shirt",
            "price": "10.00",
            "stock": 0,
            "low_stock_threshold": 0,
            "variants": [
                {
                    "option_values": {"Color": "Red", "Size": "M"},
                    "sku": "TS-RED-M",
                    "barcode": "8850001",
                    "price": "12.00",
                    "stock": 3,
                    "low_stock_threshold": 1,
                },
                {
                    "option_values": {"Color": "Blue", "Size": "L"},
                    "sku": "TS-BLUE-L",
                    "barcode": "8850002",
                    "price": "14.00",
                    "stock": 5,
                    "low_stock_threshold": 2,
                },
            ],
        },
        headers=seller.headers,
    )

    assert response.status_code == 201, response.text
    product = response.json()
    assert [variant["name"] for variant in product["variants"]] == ["Red / M", "Blue / L"]
    assert (product["price"], product["stock"], product["low_stock_threshold"]) == (
        "12.00", 8, 3,
    )


def test_variant_identity_is_unique_and_tenant_scoped(client, seller, other_seller):
    product = seller.product()
    created = create_variant(
        client, seller, product["id"], options={"Color": "Red"}, sku="RED-1"
    )
    assert created.status_code == 201, created.text

    duplicate = create_variant(
        client, seller, product["id"], options={"Color": "Red"}, sku="RED-2"
    )
    assert duplicate.status_code == 409
    stolen = client.patch(
        f"/api/v1/products/{product['id']}/variants/{created.json()['id']}",
        json={"price": "0.01"},
        headers=other_seller.headers,
    )
    assert stolen.status_code == 404

    protected = client.delete(
        f"/api/v1/products/{product['id']}/variants/{created.json()['id']}",
        headers=seller.headers,
    )
    assert protected.status_code == 409
    assert "stock to zero" in protected.json()["detail"]


def test_multi_variant_order_requires_choice_and_snapshots_variant(client, seller):
    product = seller.product(name="T-shirt", price="10.00", stock=2)
    red = create_variant(
        client, seller, product["id"], options={"Color": "Red", "Size": "M"},
        price="14.00", stock=4, sku="RED-M",
    ).json()
    customer = seller.customer()

    ambiguous = seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])
    assert ambiguous.status_code == 400
    assert "Choose a variant" in ambiguous.json()["detail"]

    order = seller.order(customer["id"], [{
        "product_id": product["id"], "variant_id": red["id"], "quantity": 3,
    }])
    assert order.status_code == 201, order.text
    item = order.json()["items"][0]
    assert item["variant_id"] == red["id"]
    assert item["variant_name"] == "Red / M"
    assert item["variant_sku"] == "RED-M"
    assert item["variant_options"] == {"Color": "Red", "Size": "M"}
    assert (item["unit_price"], item["subtotal"]) == ("14.00", "42.00")
    refreshed = client.get(
        f"/api/v1/products/{product['id']}", headers=seller.headers
    ).json()
    selected = next(variant for variant in refreshed["variants"] if variant["id"] == red["id"])
    assert (selected["stock"], selected["reserved_stock"]) == (1, 3)


def test_adjustment_and_image_assignment_target_one_variant(client, seller):
    product = seller.product(stock=2)
    red = create_variant(
        client, seller, product["id"], options={"Color": "Red"}, stock=4
    ).json()
    adjusted = client.post(
        f"/api/v1/inventory/products/{product['id']}/adjustments",
        json={"variant_id": red["id"], "quantity_delta": 3, "reason": "Supplier delivery"},
        headers=seller.headers,
    )
    assert adjusted.status_code == 200, adjusted.text
    refreshed = adjusted.json()
    target = next(variant for variant in refreshed["variants"] if variant["id"] == red["id"])
    assert target["stock"] == 7

    uploaded = client.post(
        f"/api/v1/products/{product['id']}/images",
        files=[("files", ("red.png", PNG, "image/png"))],
        headers=seller.headers,
    ).json()[0]
    assigned = client.patch(
        f"/api/v1/products/{product['id']}/images/{uploaded['id']}/variant",
        json={"variant_id": red["id"]},
        headers=seller.headers,
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["variant_id"] == red["id"]


def test_assistant_catalogue_lists_variants_and_requires_options(client, seller, db):
    product = seller.product(name="T-shirt", stock=2)
    create_variant(
        client, seller, product["id"], options={"Color": "Red", "Size": "M"}, stock=4
    )
    refreshed = client.get(
        f"/api/v1/products/{product['id']}", headers=seller.headers
    ).json()
    from app.models.product import Product
    stored = db.query(Product).filter(Product.id == refreshed["id"]).one()
    user = db.query(User).filter(User.email == seller.email).one()

    prompt = build_system_prompt(user, [stored])

    assert "Color: Red" in prompt
    assert "Size: M" in prompt
    assert "ask for every required option" in prompt
