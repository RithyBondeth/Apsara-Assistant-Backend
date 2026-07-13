"""Product CRUD + tenant isolation tests."""
from __future__ import annotations


def _register_login(client, email):
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": "X"},
    )
    token = client.post(
        "/api/v1/auth/login", data={"username": email, "password": "password123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_product_crud(auth_client):
    client, _token, _uid = auth_client

    # create
    r = client.post("/api/v1/products/", json={"name": "Krama", "price": "12.50", "stock": 5})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["name"] == "Krama"

    # list
    r = client.get("/api/v1/products/")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # get
    assert client.get(f"/api/v1/products/{pid}").status_code == 200

    # update
    r = client.patch(f"/api/v1/products/{pid}", json={"price": "15.00"})
    assert r.status_code == 200
    assert r.json()["price"] == "15.00"

    # delete
    assert client.delete(f"/api/v1/products/{pid}").status_code == 204
    assert client.get(f"/api/v1/products/{pid}").status_code == 404


def test_products_are_tenant_isolated(client):
    # user A creates a product
    a_headers = _register_login(client, "a@shop.com")
    r = client.post(
        "/api/v1/products/",
        json={"name": "A-only", "price": "9.99", "stock": 1},
        headers=a_headers,
    )
    a_pid = r.json()["id"]

    # user B cannot see or fetch it
    b_headers = _register_login(client, "b@shop.com")
    assert client.get("/api/v1/products/", headers=b_headers).json() == []
    assert client.get(f"/api/v1/products/{a_pid}", headers=b_headers).status_code == 404
