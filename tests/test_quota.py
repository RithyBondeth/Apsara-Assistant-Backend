"""The daily ceiling on assistant replies."""

import pytest

from app.models.ai_usage import AiUsage
from app.services import quota
from tests import ai, webhooks as wh


@pytest.fixture
def limit(monkeypatch):
    def _set(n):
        monkeypatch.setattr(quota.settings, "AI_DAILY_REPLY_LIMIT", n)
    return _set


def test_spending_counts_up(db, seller, client, limit):
    limit(10)
    user_id = client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]

    assert quota.spend_reply(db, user_id) is True
    assert quota.spend_reply(db, user_id) is True

    assert quota.used_today(db, user_id) == 2
    assert db.query(AiUsage).count() == 1, "one row per seller per day"


def test_the_last_allowed_reply_still_goes_through(db, seller, client, limit):
    limit(2)
    user_id = client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]

    assert quota.spend_reply(db, user_id) is True
    assert quota.spend_reply(db, user_id) is True
    assert quota.spend_reply(db, user_id) is False


def test_a_zero_limit_means_no_ceiling(db, seller, client, limit):
    limit(0)
    user_id = client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]

    for _ in range(50):
        assert quota.spend_reply(db, user_id) is True
    assert quota.used_today(db, user_id) == 0, "nothing is counted when disabled"


def test_sellers_have_separate_allowances(db, client, seller, other_seller, limit):
    limit(1)
    mine = client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]
    theirs = client.get("/api/v1/auth/me", headers=other_seller.headers).json()["id"]

    assert quota.spend_reply(db, mine) is True
    assert quota.spend_reply(db, mine) is False
    assert quota.spend_reply(db, theirs) is True, "one busy seller must not "\
        "exhaust another's allowance"


# ── Applied to real traffic ──────────────────────────────────────────────────

def test_an_over_budget_inbound_message_is_stored_unanswered(client, seller, db, limit):
    """The customer's words are still recorded — the seller can answer by hand."""
    limit(1)
    wh.connect(client, seller, external_id="page-1")

    with wh.sends(), ai.replies("first"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "one",
                                                       mid="mid.1"))
    with wh.sends() as sent, ai.replies("second"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "two",
                                                       mid="mid.2"))

    assert sent == [], "the second reply must not have been sent"
    conv = client.get("/api/v1/conversations/", headers=seller.headers).json()[0]
    thread = client.get(f"/api/v1/conversations/{conv['id']}/messages",
                        headers=seller.headers).json()
    assert [(m["sender_type"], m["content"]) for m in thread] == [
        ("customer", "one"), ("assistant", "first"), ("customer", "two")]


def test_the_dashboard_endpoint_is_held_to_the_same_ceiling(client, seller, limit):
    """Otherwise the limit is trivially sidestepped by chatting from the app."""
    limit(1)
    conversation = seller.conversation()

    with ai.replies("ok"):
        first = client.post(f"/api/v1/chat/{conversation['id']}",
                            json={"message": "one"}, headers=seller.headers)
        second = client.post(f"/api/v1/chat/{conversation['id']}",
                             json={"message": "two"}, headers=seller.headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "limit" in second.json()["detail"].lower()


def test_a_refused_reply_still_leaves_the_message(client, seller, limit):
    limit(0 if False else 1)
    conversation = seller.conversation()
    with ai.replies("ok"):
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "one"},
                    headers=seller.headers)
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "two"},
                    headers=seller.headers)

    thread = client.get(f"/api/v1/conversations/{conversation['id']}/messages",
                        headers=seller.headers).json()
    assert [m["content"] for m in thread] == ["one", "ok", "two"]


def test_a_failed_generation_still_costs(client, seller, db, limit):
    """The spend happens on asking, not on succeeding — otherwise a model that
    keeps erroring is an uncapped bill."""
    from openai import APIConnectionError
    limit(10)
    conversation = seller.conversation()
    user_id = client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]

    with ai.fails(APIConnectionError(request=None)):
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                        headers=seller.headers)

    assert r.status_code == 503
    assert quota.used_today(db, user_id) == 1
