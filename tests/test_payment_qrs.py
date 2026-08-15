"""A shop's named payment QR collection and assistant default selection."""

from app.models.user import User
from app.services.payment_qrs import default_payment_qr_url

PNG = b"\x89PNG\r\n\x1a\n" + b"qr-image-bytes"


def create_qr(client, seller, name="ABA USD", **fields):
    data = {"name": name, **fields}
    return client.post(
        "/api/v1/payment-qrs/",
        data=data,
        files={"file": ("qr.png", PNG, "image/png")},
        headers=seller.headers,
    )


def test_shop_can_store_multiple_qrs_and_choose_default(client, seller):
    first = create_qr(client, seller, bank_name="ABA", currency="USD")
    second = create_qr(client, seller, name="Wing KHR", bank_name="Wing", currency="KHR")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["is_default"] is True
    assert second.json()["is_default"] is False

    selected = client.patch(
        f"/api/v1/payment-qrs/{second.json()['id']}",
        json={"is_default": True},
        headers=seller.headers,
    )
    assert selected.status_code == 200, selected.text
    listed = client.get("/api/v1/payment-qrs/", headers=seller.headers).json()
    assert [qr["id"] for qr in listed if qr["is_default"]] == [second.json()["id"]]


def test_public_qr_content_can_be_fetched_by_chat_platform(client, seller):
    qr = create_qr(client, seller).json()

    response = client.get(qr["url"])

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_disabling_default_promotes_another_active_qr(client, seller):
    first = create_qr(client, seller).json()
    second = create_qr(client, seller, name="Wing").json()

    response = client.patch(
        f"/api/v1/payment-qrs/{first['id']}",
        json={"is_active": False},
        headers=seller.headers,
    )

    assert response.status_code == 200, response.text
    listed = client.get("/api/v1/payment-qrs/", headers=seller.headers).json()
    assert [qr["id"] for qr in listed if qr["is_default"]] == [second["id"]]


def test_deleting_default_promotes_next_active_qr(client, seller):
    first = create_qr(client, seller).json()
    second = create_qr(client, seller, name="Wing").json()

    response = client.delete(f"/api/v1/payment-qrs/{first['id']}", headers=seller.headers)

    assert response.status_code == 204
    listed = client.get("/api/v1/payment-qrs/", headers=seller.headers).json()
    assert listed[0]["id"] == second["id"]
    assert listed[0]["is_default"] is True


def test_payment_qrs_are_tenant_scoped(client, seller, other_seller):
    qr = create_qr(client, seller).json()

    response = client.patch(
        f"/api/v1/payment-qrs/{qr['id']}",
        json={"name": "Mine now"},
        headers=other_seller.headers,
    )

    assert response.status_code == 404
    assert client.get("/api/v1/payment-qrs/", headers=other_seller.headers).json() == []


def test_rejects_spoofed_qr_image(client, seller):
    response = client.post(
        "/api/v1/payment-qrs/",
        data={"name": "Bad"},
        files={"file": ("bad.png", b"not an image", "image/png")},
        headers=seller.headers,
    )
    assert response.status_code == 415


def test_shop_cannot_exceed_five_payment_qrs(client, seller):
    for index in range(5):
        assert create_qr(client, seller, name=f"QR {index}").status_code == 201

    response = create_qr(client, seller, name="Sixth")

    assert response.status_code == 400
    assert "at most 5" in response.json()["detail"]


def test_uploaded_default_supersedes_legacy_link(client, seller, db):
    qr = create_qr(client, seller).json()
    user = db.query(User).filter(User.email == seller.email).one()
    user.payment_qr_url = "https://legacy.example/qr.png"
    db.commit()
    db.refresh(user)

    assert default_payment_qr_url(user) == qr["url"]
