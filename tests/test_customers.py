"""Customer platform validation + update round-trip tests."""
from __future__ import annotations


def test_create_customer_rejects_unsupported_platform(auth_client):
    client, _token, _uid = auth_client

    for platform in ("facebook", "tiktok"):
        r = client.post(
            "/api/v1/customers/", json={"name": "Chan Sopheak", "platform": platform}
        )
        assert r.status_code == 400, f"{platform!r} should be rejected: {r.text}"
        assert r.json()["detail"]["code"] == "unsupported_platform"


def test_create_customer_allows_supported_or_absent_platform(auth_client):
    client, _token, _uid = auth_client

    # A customer added by hand needn't be tied to a channel at all.
    r = client.post("/api/v1/customers/", json={"name": "Walk-in"})
    assert r.status_code == 201, r.text
    assert r.json()["platform"] is None

    r = client.post(
        "/api/v1/customers/",
        json={"name": "Sok Dara", "platform": "telegram", "platform_id": "tg-1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["platform"] == "telegram"


def test_update_customer_persists_platform_fields(auth_client):
    """The edit form exposes platform + platform_id, so they must round-trip."""
    client, _token, _uid = auth_client
    cid = client.post("/api/v1/customers/", json={"name": "Chan Sopheak"}).json()["id"]

    r = client.patch(
        f"/api/v1/customers/{cid}",
        json={"platform": "messenger", "platform_id": "psid-123"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["platform"] == "messenger"
    assert r.json()["platform_id"] == "psid-123"

    # And it must survive a re-read, not just echo back.
    fresh = client.get(f"/api/v1/customers/{cid}").json()
    assert fresh["platform"] == "messenger"
    assert fresh["platform_id"] == "psid-123"


def test_update_customer_rejects_unsupported_platform(auth_client):
    client, _token, _uid = auth_client
    cid = client.post("/api/v1/customers/", json={"name": "Chan Sopheak"}).json()["id"]

    r = client.patch(f"/api/v1/customers/{cid}", json={"platform": "tiktok"})
    assert r.status_code == 400, r.text

    # A partial update that doesn't mention platform must still work.
    r = client.patch(f"/api/v1/customers/{cid}", json={"name": "Chan S."})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Chan S."
