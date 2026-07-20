"""Free-text search across the list endpoints.

The escaping cases matter more than the happy path: LIKE treats % and _ as
wildcards, so an unescaped term silently returns the wrong rows rather than
erroring, which is the kind of bug nobody notices until a customer complains.
"""
from __future__ import annotations

from app.core.search import like_pattern


def _make_product(client, name: str, description: str = ""):
    return client.post(
        "/api/v1/products/",
        json={"name": name, "description": description, "price": 5, "stock": 50},
    ).json()


def _make_customer(client, name: str, email: str | None = None):
    payload = {"name": name}
    if email:
        payload["email"] = email
    return client.post("/api/v1/customers/", json=payload).json()


def test_product_search_matches_name_case_insensitively(auth_client):
    client, _token, _uid = auth_client
    _make_product(client, "Khmer Silk Scarf")
    _make_product(client, "Kampot Pepper")

    found = client.get("/api/v1/products/", params={"search": "silk"}).json()

    assert found["total"] == 1
    assert found["items"][0]["name"] == "Khmer Silk Scarf"


def test_product_search_also_matches_the_description(auth_client):
    client, _token, _uid = auth_client
    _make_product(client, "Scarf", description="Handwoven in Takeo province")
    _make_product(client, "Pepper", description="From Kampot")

    found = client.get("/api/v1/products/", params={"search": "takeo"}).json()

    assert [p["name"] for p in found["items"]] == ["Scarf"]


def test_blank_search_returns_everything(auth_client):
    """A cleared search box must not filter — it isn't a match-nothing query."""
    client, _token, _uid = auth_client
    _make_product(client, "Scarf")
    _make_product(client, "Pepper")

    for term in ("", "   "):
        found = client.get("/api/v1/products/", params={"search": term}).json()
        assert found["total"] == 2, f"blank term {term!r} filtered the list"


def test_search_term_wildcards_are_escaped(auth_client):
    """"%" is a literal to the seller typing it, not "match anything"."""
    client, _token, _uid = auth_client
    _make_product(client, "50% off bundle")
    _make_product(client, "Plain scarf")

    found = client.get("/api/v1/products/", params={"search": "50%"}).json()
    assert found["total"] == 1

    # The real tell: a lone "%" must mean "rows containing a percent sign", not
    # "every row". Unescaped it matches both products.
    literal = client.get("/api/v1/products/", params={"search": "%"}).json()
    assert [p["name"] for p in literal["items"]] == ["50% off bundle"]


def test_underscore_is_escaped_too(auth_client):
    client, _token, _uid = auth_client
    _make_product(client, "size_guide")
    _make_product(client, "sizeXguide")

    found = client.get("/api/v1/products/", params={"search": "size_guide"}).json()

    assert [p["name"] for p in found["items"]] == ["size_guide"]


def test_search_respects_tenant_isolation(auth_client, client):
    """Search must not become a way to read another seller's catalogue."""
    owner, _token, _uid = auth_client
    _make_product(owner, "Secret Silk")

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "other@example.com",
            "password": "OtherPass123",
            "full_name": "Other",
            "business_name": "Other Shop",
        },
    )
    token = client.post(
        "/api/v1/auth/login",
        data={"username": "other@example.com", "password": "OtherPass123"},
    ).json()["access_token"]

    found = client.get(
        "/api/v1/products/",
        params={"search": "silk"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert found["total"] == 0


def test_customer_search_spans_name_and_email(auth_client):
    client, _token, _uid = auth_client
    _make_customer(client, "Sok Dara", email="dara@example.com")
    _make_customer(client, "Chan Sopheak", email="sopheak@example.com")

    by_name = client.get("/api/v1/customers/", params={"search": "dara"}).json()
    by_email = client.get("/api/v1/customers/", params={"search": "sopheak@"}).json()

    assert [c["name"] for c in by_name["items"]] == ["Sok Dara"]
    assert [c["name"] for c in by_email["items"]] == ["Chan Sopheak"]


def test_conversation_search_matches_the_customer_name(auth_client):
    client, _token, _uid = auth_client
    dara = _make_customer(client, "Sok Dara")
    sopheak = _make_customer(client, "Chan Sopheak")
    for customer in (dara, sopheak):
        client.post(
            "/api/v1/conversations/",
            json={"customer_id": customer["id"], "platform": "website"},
        )

    found = client.get("/api/v1/conversations/", params={"search": "sopheak"}).json()

    assert found["total"] == 1
    assert found["items"][0]["customer_id"] == sopheak["id"]


def test_order_search_matches_the_customer_name(auth_client):
    client, _token, _uid = auth_client
    product = _make_product(client, "Scarf")
    dara = _make_customer(client, "Sok Dara")
    sopheak = _make_customer(client, "Chan Sopheak")
    for customer in (dara, sopheak):
        client.post(
            "/api/v1/orders/",
            json={
                "customer_id": customer["id"],
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )

    found = client.get("/api/v1/orders/", params={"search": "dara"}).json()

    assert found["total"] == 1
    assert found["items"][0]["customer_id"] == dara["id"]


def test_search_pagination_total_counts_only_matches(auth_client):
    """The envelope's total must reflect the search, not the whole table.

    Counting the unfiltered query would make the pager offer pages that come
    back empty.
    """
    client, _token, _uid = auth_client
    for i in range(5):
        _make_product(client, f"Silk item {i}")
    for i in range(3):
        _make_product(client, f"Pepper item {i}")

    page = client.get(
        "/api/v1/products/", params={"search": "silk", "skip": 0, "limit": 2}
    ).json()

    assert page["total"] == 5
    assert len(page["items"]) == 2


def test_like_pattern_escapes_the_escape_character(auth_client):
    """A backslash in the term must not turn the next character into an escape."""
    assert like_pattern("a\\b") == "%a\\\\b%"
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern(" pad ") == "%pad%"
