"""Uploaded product galleries and their public, bounded media delivery."""

from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import selectinload

from app.models.product import Product

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image-bytes"
JPEG = b"\xff\xd8\xff" + b"safe-jpeg-bytes"


def upload(client, seller, product_id, files):
    return client.post(
        f"/api/v1/products/{product_id}/images",
        files=[("files", (name, content, content_type)) for name, content, content_type in files],
        headers=seller.headers,
    )


def test_uploads_multiple_images_and_exposes_primary_in_product(client, seller):
    product = seller.product()

    response = upload(client, seller, product["id"], [
        ("front.png", PNG, "image/png"),
        ("detail.jpg", JPEG, "image/jpeg"),
    ])

    assert response.status_code == 201, response.text
    images = response.json()
    assert [image["position"] for image in images] == [0, 1]
    assert [image["is_primary"] for image in images] == [True, False]
    stored = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert [image["id"] for image in stored["images"]] == [image["id"] for image in images]


def test_uploaded_product_image_is_public_but_strictly_served(client, seller):
    product = seller.product()
    image = upload(client, seller, product["id"], [("front.png", PNG, "image/png")]).json()[0]

    response = client.get(image["url"])

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_catalog_gallery_does_not_load_binary_bytes(client, seller, db):
    product = seller.product()
    upload(client, seller, product["id"], [("front.png", PNG, "image/png")])

    stored = (
        db.query(Product)
        .options(selectinload(Product.images))
        .filter(Product.id == UUID(product["id"]))
        .one()
    )

    assert "blob" in inspect(stored.images[0]).unloaded


def test_reorders_gallery_and_changes_primary(client, seller):
    product = seller.product()
    images = upload(client, seller, product["id"], [
        ("one.png", PNG, "image/png"),
        ("two.jpg", JPEG, "image/jpeg"),
    ]).json()

    response = client.put(
        f"/api/v1/products/{product['id']}/images/order",
        json={
            "image_ids": [images[1]["id"], images[0]["id"]],
            "primary_image_id": images[1]["id"],
        },
        headers=seller.headers,
    )

    assert response.status_code == 200, response.text
    assert [image["id"] for image in response.json()] == [images[1]["id"], images[0]["id"]]
    assert [image["is_primary"] for image in response.json()] == [True, False]


def test_deleting_primary_promotes_next_image(client, seller):
    product = seller.product()
    images = upload(client, seller, product["id"], [
        ("one.png", PNG, "image/png"),
        ("two.jpg", JPEG, "image/jpeg"),
    ]).json()

    response = client.delete(
        f"/api/v1/products/{product['id']}/images/{images[0]['id']}",
        headers=seller.headers,
    )

    assert response.status_code == 204
    stored = client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()
    assert len(stored["images"]) == 1
    assert stored["images"][0]["is_primary"] is True
    assert client.get(images[0]["url"]).status_code == 404


def test_rejects_spoofed_and_excess_images(client, seller):
    product = seller.product()
    spoofed = upload(client, seller, product["id"], [
        ("not-really.png", b"<script>alert(1)</script>", "image/png"),
    ])
    assert spoofed.status_code == 415

    too_many = upload(
        client,
        seller,
        product["id"],
        [(f"{index}.png", PNG, "image/png") for index in range(9)],
    )
    assert too_many.status_code == 400


def test_other_seller_cannot_manage_product_gallery(client, seller, other_seller):
    product = seller.product()

    response = upload(
        client, other_seller, product["id"], [("stolen.png", PNG, "image/png")]
    )

    assert response.status_code == 404
    assert client.get(f"/api/v1/products/{product['id']}", headers=seller.headers).json()["images"] == []
