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
    assert len(r.json()["items"]) == 1
    assert r.json()["total"] == 1

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
    assert client.get("/api/v1/products/", headers=b_headers).json() == {
        "items": [],
        "total": 0,
    }
    assert client.get(f"/api/v1/products/{a_pid}", headers=b_headers).status_code == 404


def _make_products(client, n, prefix="P"):
    for i in range(n):
        r = client.post(
            "/api/v1/products/",
            json={"name": f"{prefix}{i:03d}", "price": "1.00", "stock": 1},
        )
        assert r.status_code == 201, r.text


def test_list_reports_total_beyond_the_page(auth_client):
    """The bug this fixes: a page of results used to be indistinguishable from
    the whole catalogue, so a seller with more products than the page size had
    no way to know — or reach — the rest."""
    client, _t, _u = auth_client
    _make_products(client, 25)

    r = client.get("/api/v1/products/", params={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 10
    assert body["total"] == 25


def test_paging_reaches_every_product_exactly_once(auth_client):
    """Walking the pages must yield the full catalogue with no gaps or repeats.

    An off-by-one in the offset would silently drop or duplicate a product,
    which is the same class of invisible data loss as the original bug.
    """
    client, _t, _u = auth_client
    _make_products(client, 25)

    seen = []
    for skip in (0, 10, 20):
        page = client.get("/api/v1/products/", params={"skip": skip, "limit": 10}).json()
        seen.extend(p["name"] for p in page["items"])

    assert len(seen) == 25
    assert len(set(seen)) == 25
    assert sorted(seen) == [f"P{i:03d}" for i in range(25)]


def test_page_past_the_end_is_empty_not_an_error(auth_client):
    client, _t, _u = auth_client
    _make_products(client, 5)

    body = client.get("/api/v1/products/", params={"skip": 100, "limit": 10}).json()
    assert body["items"] == []
    assert body["total"] == 5


def test_total_counts_only_the_callers_products(auth_client, client):
    """`total` drives the seller's pager, so another tenant's rows must not
    inflate it — that would render page links to products they can't see."""
    owner, _t, _u = auth_client
    _make_products(owner, 3, prefix="MINE")

    stranger = _register_login(client, "stranger@shop.com")
    body = client.get("/api/v1/products/", headers=stranger).json()
    assert body["total"] == 0


def test_total_respects_the_active_only_filter(auth_client):
    """`total` must count the same set the items come from, or the pager will
    advertise pages that render empty."""
    client, _t, _u = auth_client
    _make_products(client, 4)
    pid = client.get("/api/v1/products/").json()["items"][0]["id"]
    client.patch(f"/api/v1/products/{pid}", json={"is_active": False})

    active = client.get("/api/v1/products/").json()
    assert active["total"] == 3
    assert len(active["items"]) == 3

    every = client.get("/api/v1/products/", params={"active_only": False}).json()
    assert every["total"] == 4


def test_oversized_limit_is_rejected(auth_client):
    """Without a ceiling, `limit=1000000` is an easy way to make the server
    serialise a seller's entire catalogue in one request."""
    client, _t, _u = auth_client
    assert client.get("/api/v1/products/", params={"limit": 10_000}).status_code == 422
    assert client.get("/api/v1/products/", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/products/", params={"skip": -1}).status_code == 422
