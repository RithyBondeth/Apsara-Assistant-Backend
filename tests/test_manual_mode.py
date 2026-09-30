"""The assistant takes a thread back after the seller has gone quiet.

Answering a customer means "I have got this one", not "never speak on this
thread again" — a shop that answered ten customers on Monday should not find
ten dead threads on Tuesday. Pressing Take over is the other case, and that
one stands until the seller hands the thread back.
"""

from datetime import timedelta
from unittest import mock

import pytest

from app.core.clock import utcnow
from app.models.conversation import Conversation
from app.models.user import User
from app.services import handoff
from tests import ai, webhooks as wh
from tests.test_seller_alerts import alerts, customer_says, link, the_conversation


def seller_replies(client, seller, conversation_id, content="Yes, we have it."):
    with mock.patch("app.api.v1.endpoints.conversations.send_reply", return_value=True):
        r = client.post(f"/api/v1/conversations/{conversation_id}/messages",
                        json={"content": content}, headers=seller.headers)
    assert r.status_code == 201, r.text
    return r.json()


def went_quiet(db, conversation_id, hours):
    """Backdate the seller's last word, as if they had not written since."""
    conversation = db.query(Conversation).filter(Conversation.id == conversation_id).one()
    conversation.last_seller_message_at = utcnow() - timedelta(hours=hours)
    db.commit()


def customer_writes_again(client, integration, update_id):
    return customer_says(client, integration, "are you there?", update_id=update_id)


# ── The default: a reply expires ─────────────────────────────────────────────

def test_the_assistant_resumes_after_twelve_hours_of_seller_silence(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies("Bat!"):
        customer_says(client, integration, "do you have krama?", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])
    assert the_conversation(client, seller)["handling_mode"] == "manual"
    assert the_conversation(client, seller)["manual_mode_source"] == "reply"

    went_quiet(db, conversation["id"], hours=13)
    with wh.sends() as to_customer, alerts(), ai.replies("Bat, mean nov ban!") as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured != {}, "the assistant should have answered"
    assert to_customer[-1]["text"] == "Bat, mean nov ban!"
    resumed = the_conversation(client, seller)
    assert resumed["handling_mode"] == "auto"
    assert resumed["manual_mode_source"] is None
    assert resumed["assigned_user_id"] is None


def test_the_assistant_stays_quiet_inside_the_window(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])

    went_quiet(db, conversation["id"], hours=11)
    with wh.sends(), alerts() as to_seller, ai.replies() as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured == {}, "the seller is still on this one"
    assert the_conversation(client, seller)["handling_mode"] == "manual"
    assert "New message from" in to_seller[0]["text"]


def test_the_clock_restarts_each_time_the_seller_writes(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])
    went_quiet(db, conversation["id"], hours=13)

    # They come back to the thread before the customer does.
    seller_replies(client, seller, conversation["id"], "One moment.")
    with wh.sends(), alerts(), ai.replies() as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured == {}
    assert the_conversation(client, seller)["handling_mode"] == "manual"


# ── Pressing Take over is a standing instruction ─────────────────────────────

def test_an_explicit_takeover_never_expires(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    r = client.patch(f"/api/v1/conversations/{conversation['id']}",
                     json={"handling_mode": "manual"}, headers=seller.headers)
    assert r.json()["manual_mode_source"] == "explicit"

    went_quiet(db, conversation["id"], hours=500)
    with wh.sends(), alerts(), ai.replies() as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured == {}, "Take over means until further notice"
    assert the_conversation(client, seller)["handling_mode"] == "manual"


def test_handing_the_thread_back_clears_the_reason(client, seller, db):
    integration = link(client, seller, db)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])

    r = client.patch(f"/api/v1/conversations/{conversation['id']}",
                     json={"handling_mode": "auto"}, headers=seller.headers)
    assert r.json()["manual_mode_source"] is None
    assert r.json()["assigned_user_id"] is None


# ── The seller's own setting ─────────────────────────────────────────────────

def test_zero_keeps_the_old_behaviour(client, seller, db):
    """A seller who wants the assistant to stay out until they say otherwise."""
    integration = link(client, seller, db, manual_timeout_hours=0)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])

    went_quiet(db, conversation["id"], hours=1000)
    with wh.sends(), alerts(), ai.replies() as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured == {}
    assert the_conversation(client, seller)["handling_mode"] == "manual"


def test_the_window_is_the_sellers_to_set(client, seller, db):
    integration = link(client, seller, db, manual_timeout_hours=2)
    with wh.sends(), alerts(), ai.replies():
        customer_says(client, integration, "hello", update_id=1)
    conversation = the_conversation(client, seller)
    seller_replies(client, seller, conversation["id"])

    went_quiet(db, conversation["id"], hours=3)
    with wh.sends(), alerts(), ai.replies("Back!") as captured:
        customer_writes_again(client, integration, update_id=2)

    assert captured != {}
    assert the_conversation(client, seller)["handling_mode"] == "auto"


def test_the_default_is_twelve_hours_and_it_is_bounded(client, seller):
    assert client.get("/api/v1/auth/me",
                      headers=seller.headers).json()["manual_timeout_hours"] == 12
    assert client.patch("/api/v1/auth/me", json={"manual_timeout_hours": 24},
                        headers=seller.headers).json()["manual_timeout_hours"] == 24
    # 0 is meaningful (never resume); a month is the ceiling.
    assert client.patch("/api/v1/auth/me", json={"manual_timeout_hours": 0},
                        headers=seller.headers).status_code == 200
    assert client.patch("/api/v1/auth/me", json={"manual_timeout_hours": -1},
                        headers=seller.headers).status_code == 422
    assert client.patch("/api/v1/auth/me", json={"manual_timeout_hours": 721},
                        headers=seller.headers).status_code == 422


# ── The rule itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode, source, hours_quiet, expected", [
    ("manual", "reply", 13, True),
    ("manual", "reply", 11, False),
    ("manual", "explicit", 999, False),
    ("auto", None, 999, False),
    # A thread marked as a reply with no reply behind it has no clock to run.
    ("manual", "reply", None, False),
])
def test_resume_due(mode, source, hours_quiet, expected):
    conversation = Conversation(
        handling_mode=mode, manual_mode_source=source,
        last_seller_message_at=(None if hours_quiet is None
                                else utcnow() - timedelta(hours=hours_quiet)),
    )
    assert handoff.resume_due(conversation, User(manual_timeout_hours=12)) is expected
