"""The AI chat endpoint: POST /api/v1/chat/{conversation_id}."""

import uuid

import pytest
from openai import APIConnectionError

from app.services import ai_service
from tests import ai


def messages_in(client, seller, conversation_id):
    return client.get(f"/api/v1/conversations/{conversation_id}/messages",
                      headers=seller.headers).json()


# ── The shape the web client depends on ──────────────────────────────────────

def test_returns_both_messages_in_the_shape_the_client_expects(client, seller):
    conversation = seller.conversation()

    with ai.replies("Tlai 12.50."):
        r = client.post(f"/api/v1/chat/{conversation['id']}",
                        json={"message": "tlai ponman?", "message_type": "text"},
                        headers=seller.headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"customer_message", "ai_message"}
    for message in body.values():
        assert set(message) >= {"id", "conversation_id", "sender_type", "message_type",
                                "content", "created_at", "attachments"}
        assert message["conversation_id"] == conversation["id"]
        assert message["attachments"] == []

    assert body["customer_message"]["sender_type"] == "customer"
    assert body["ai_message"]["sender_type"] == "assistant"
    assert body["ai_message"]["content"] == "Tlai 12.50."


def test_persists_both_messages_in_order(client, seller):
    conversation = seller.conversation()
    with ai.replies():
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hello"},
                    headers=seller.headers)

    stored = messages_in(client, seller, conversation["id"])
    assert [m["sender_type"] for m in stored] == ["customer", "assistant"]


def test_strips_surrounding_whitespace(client, seller):
    conversation = seller.conversation()
    with ai.replies():
        r = client.post(f"/api/v1/chat/{conversation['id']}",
                        json={"message": "  spaced out  "}, headers=seller.headers)
    assert r.json()["customer_message"]["content"] == "spaced out"


# ── What the model is told ───────────────────────────────────────────────────

def test_prompt_carries_the_sellers_catalogue(client, seller):
    seller.product("Silk Scarf", "12.50", 8, description="Hand-woven")
    seller.product("Krama", "6.00", 0)
    conversation = seller.conversation()

    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=seller.headers)

    assert "Sok Silk Shop" in captured.system_prompt
    assert "• Silk Scarf — 12.50" in captured.system_prompt
    assert "Hand-woven" in captured.system_prompt
    assert "OUT OF STOCK" in captured.system_prompt
    assert captured["model"] == "gpt-4o-mini"


def test_prompt_does_not_branch_on_a_guess_at_the_language(client, seller):
    """The model mirrors the customer's script; nothing classifies it up front.

    A previous implementation ran a substring-matching detector that read
    "How much is this?" as romanized Khmer and instructed the model to answer
    in it. The prompt must now be identical whatever the customer writes.
    """
    conversation = seller.conversation()

    prompts = []
    for message in ["How much is this?", "តម្លៃប៉ុន្មាន?", "tlai ponman?"]:
        with ai.replies() as captured:
            client.post(f"/api/v1/chat/{conversation['id']}", json={"message": message},
                        headers=seller.headers)
        prompts.append(captured.system_prompt)

    assert len(set(prompts)) == 1
    assert "English in — English out" in prompts[0]
    assert "romanized Khmer out" in prompts[0]


def test_catalogue_is_scoped_to_the_seller(client, seller, other_seller):
    seller.product("Silk Scarf", "12.50", 5)
    other_seller.product("Rival Bag", "99.00", 5)
    conversation = other_seller.conversation()

    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=other_seller.headers)

    assert "Rival Bag" in captured.system_prompt
    assert "Silk Scarf" not in captured.system_prompt


def test_seller_without_products_says_so(client, seller):
    conversation = seller.conversation()
    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "mean avei?"},
                    headers=seller.headers)
    assert "No products are listed yet." in captured.system_prompt


def test_history_accumulates_and_is_replayed(client, seller):
    conversation = seller.conversation()
    for message in ["first", "second"]:
        with ai.replies("ok") as captured:
            client.post(f"/api/v1/chat/{conversation['id']}", json={"message": message},
                        headers=seller.headers)

    assert [t["role"] for t in captured.turns] == ["user", "assistant", "user"]
    assert captured.turns[-1]["content"] == "second"


def test_history_is_bounded(client, seller):
    conversation = seller.conversation()
    for i in range(HISTORY_TURNS := 15):
        with ai.replies(f"reply {i}") as captured:
            client.post(f"/api/v1/chat/{conversation['id']}", json={"message": f"msg {i}"},
                        headers=seller.headers)

    assert len(captured["messages"]) == ai_service.HISTORY_LIMIT + 1
    assert captured.turns[-1]["content"] == f"msg {HISTORY_TURNS - 1}"
    # A window may open on either role, but turns must alternate.
    roles = [t["role"] for t in captured.turns]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_seller_messages_read_as_the_assistants_own_voice(client, seller, db):
    """The thread is one shop replying, so a hand-typed seller reply is an
    assistant turn rather than a second customer."""
    conversation = seller.conversation()
    client.post(f"/api/v1/conversations/{conversation['id']}/messages",
                json={"conversation_id": conversation["id"], "sender_type": "seller",
                      "content": "we also do delivery"},
                headers=seller.headers)

    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "thanks"},
                    headers=seller.headers)

    assert captured.turns[0] == {"role": "assistant", "content": "we also do delivery"}


# ── Failure handling ─────────────────────────────────────────────────────────

def test_customer_message_survives_an_ai_outage(client, seller):
    """An earlier implementation rolled this back, destroying what the
    customer wrote whenever the model was unreachable."""
    conversation = seller.conversation()

    with ai.fails(APIConnectionError(request=None)):
        r = client.post(f"/api/v1/chat/{conversation['id']}",
                        json={"message": "are you there?"}, headers=seller.headers)

    assert r.status_code == 503
    stored = messages_in(client, seller, conversation["id"])
    assert len(stored) == 1
    assert stored[0]["content"] == "are you there?"
    assert stored[0]["sender_type"] == "customer"


def test_upstream_error_text_never_reaches_the_client(client, seller):
    conversation = seller.conversation()
    secret = "sk-live-SHOULD-NOT-APPEAR"

    with ai.fails(APIConnectionError(request=None)):
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                        headers=seller.headers)

    assert secret not in r.text
    assert "openai" not in r.text.lower()
    assert r.json()["detail"] == "The AI assistant is unavailable right now. Please try again."


def test_missing_api_key_is_a_503_not_a_500(client, seller, monkeypatch):
    conversation = seller.conversation()
    monkeypatch.setattr(ai_service.settings, "OPENAI_API_KEY", "")

    r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=seller.headers)

    assert r.status_code == 503
    assert "OPENAI_API_KEY" in r.json()["detail"]


def test_empty_reply_from_the_model_is_rejected(client, seller):
    conversation = seller.conversation()
    with ai.replies("   "):
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                        headers=seller.headers)
    assert r.status_code == 503
    assert "empty" in r.json()["detail"]


# ── Guards ───────────────────────────────────────────────────────────────────

def test_closed_conversation_is_refused(client, seller):
    conversation = seller.conversation()
    client.patch(f"/api/v1/conversations/{conversation['id']}", json={"status": "closed"},
                 headers=seller.headers)

    with ai.replies():
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                        headers=seller.headers)

    assert r.status_code == 400
    assert "closed" in r.json()["detail"]


@pytest.mark.parametrize("payload", [{"message": ""}, {"message_type": "text"},
                                     {"message": None}])
def test_malformed_bodies_are_rejected(client, seller, payload):
    conversation = seller.conversation()
    r = client.post(f"/api/v1/chat/{conversation['id']}", json=payload,
                    headers=seller.headers)
    assert r.status_code == 422


def test_whitespace_only_message_stores_nothing(client, seller):
    conversation = seller.conversation()
    with ai.replies():
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "   "},
                        headers=seller.headers)

    assert r.status_code == 400
    assert messages_in(client, seller, conversation["id"]) == []


def test_another_seller_cannot_reach_the_conversation(client, seller, other_seller):
    conversation = seller.conversation()
    with ai.replies():
        r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                        headers=other_seller.headers)
    assert r.status_code == 404


def test_unknown_conversation_is_404(client, seller):
    with ai.replies():
        r = client.post(f"/api/v1/chat/{uuid.uuid4()}", json={"message": "hi"},
                        headers=seller.headers)
    assert r.status_code == 404


def test_requires_authentication(client, seller):
    conversation = seller.conversation()
    r = client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"})
    assert r.status_code == 401
