"""Unified inbox ownership, unread state, notes, tags, and reporting."""

from unittest import mock

from app.models.conversation import ConversationNote, ConversationTag
from app.models.message import Message
from tests import ai, webhooks as wh


def first_conversation(client, seller):
    response = client.get("/api/v1/conversations/", headers=seller.headers)
    assert response.status_code == 200, response.text
    return response.json()[0]


def test_inbound_message_is_unread_and_mark_read_clears_it(client, seller):
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies("Hello"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "Hi"))

    conversation = first_conversation(client, seller)
    assert conversation["source"] == "channel"
    assert conversation["unread_count"] == 1
    assert conversation["first_customer_message_at"] is not None
    assert conversation["first_response_at"] is not None

    read = client.post(
        f"/api/v1/conversations/{conversation['id']}/read", headers=seller.headers
    )
    assert read.status_code == 200, read.text
    assert read.json()["unread_count"] == 0
    assert read.json()["last_read_at"] is not None


def test_manual_takeover_stops_ai_for_only_that_thread(client, seller, db):
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies("First reply"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "First"))
    conversation = first_conversation(client, seller)

    takeover = client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        json={"handling_mode": "manual"},
        headers=seller.headers,
    )
    assert takeover.status_code == 200, takeover.text
    assert takeover.json()["assigned_user_id"] is not None

    with wh.sends() as sent, mock.patch(
        "app.services.inbound.generate_ai_reply"
    ) as generate:
        wh.post_messenger(
            client,
            wh.messenger_payload("page-1", "psid-1", "Need a person", mid="mid.2"),
        )

    assert sent == []
    generate.assert_not_called()
    assert [row.sender_type for row in db.query(Message).order_by(Message.created_at)] == [
        "customer", "assistant", "customer"
    ]


def test_pending_thread_receives_follow_up_instead_of_splitting(client, seller, db):
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies("First reply"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-1", "First"))
    conversation = first_conversation(client, seller)
    client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        json={"status": "pending"},
        headers=seller.headers,
    )

    with wh.sends(), ai.replies("Follow-up reply"):
        wh.post_messenger(
            client,
            wh.messenger_payload("page-1", "psid-1", "Follow up", mid="mid.followup"),
        )

    assert db.query(Message).count() == 4
    assert len(client.get("/api/v1/conversations/", headers=seller.headers).json()) == 1


def test_notes_and_tags_are_returned_in_detail_and_tenant_scoped(
    client, seller, other_seller, db
):
    conversation = seller.conversation()
    note = client.post(
        f"/api/v1/conversations/{conversation['id']}/notes",
        json={"content": "  Customer prefers evening delivery.  "},
        headers=seller.headers,
    )
    tag = client.post(
        f"/api/v1/conversations/{conversation['id']}/tags",
        json={"name": " VIP "},
        headers=seller.headers,
    )
    assert note.status_code == 201, note.text
    assert note.json()["content"] == "Customer prefers evening delivery."
    assert tag.status_code == 201, tag.text
    assert tag.json()["name"] == "vip"

    duplicate = client.post(
        f"/api/v1/conversations/{conversation['id']}/tags",
        json={"name": "vip"},
        headers=seller.headers,
    )
    assert duplicate.status_code == 201
    assert db.query(ConversationTag).count() == 1

    detail = client.get(
        f"/api/v1/conversations/{conversation['id']}", headers=seller.headers
    ).json()
    assert [item["name"] for item in detail["tags"]] == ["vip"]
    assert [item["content"] for item in detail["notes"]] == [
        "Customer prefers evening delivery."
    ]

    forbidden = client.post(
        f"/api/v1/conversations/{conversation['id']}/notes",
        json={"content": "Intrusion"},
        headers=other_seller.headers,
    )
    assert forbidden.status_code == 404
    assert db.query(ConversationNote).count() == 1


def test_inbox_filters_and_metrics_include_only_live_threads(client, seller):
    seller.conversation()  # A rehearsal is visible in the list, not live-inbox metrics.
    wh.connect(client, seller, external_id="page-1")
    with wh.sends(), ai.replies("Hello"):
        wh.post_messenger(client, wh.messenger_payload("page-1", "psid-live", "Hi"))
    live = first_conversation(client, seller)
    client.patch(
        f"/api/v1/conversations/{live['id']}",
        json={"handling_mode": "manual"},
        headers=seller.headers,
    )

    unread = client.get(
        "/api/v1/conversations/", params={"unread_only": True}, headers=seller.headers
    ).json()
    mine = client.get(
        "/api/v1/conversations/", params={"assignment": "me"}, headers=seller.headers
    ).json()
    assert [item["id"] for item in unread] == [live["id"]]
    assert [item["id"] for item in mine] == [live["id"]]

    metrics = client.get("/api/v1/conversations/metrics", headers=seller.headers)
    assert metrics.status_code == 200, metrics.text
    payload = metrics.json()
    assert payload["total"] == 1
    assert payload["open"] == 1
    assert payload["unread"] == 1
    assert payload["manual"] == 1
    assert payload["average_first_response_seconds"] is not None
