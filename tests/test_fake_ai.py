"""AI_FAKE: the local demo stand-in for the Claude API."""

import pytest

from app.core.config import Settings
from app.services import ai_service, fake_ai
from app.services.ai_service import (
    NEEDS_SELLER_MARKER,
    PAYMENT_QR_MARKER,
    generate_ai_reply,
    generate_order_draft,
)
from app.services.receipts import build_receipt_messages, read_receipt


@pytest.fixture
def fake(monkeypatch):
    """Demo mode with no key at all, as a developer would run it."""
    monkeypatch.setattr(ai_service.settings, "AI_FAKE", True)
    monkeypatch.setattr(ai_service.settings, "ANTHROPIC_API_KEY", "")
    ai_service._client.cache_clear()
    yield
    ai_service._client.cache_clear()


SYSTEM = """PRODUCT CATALOG
• Silk Scarf — 12.50 USD (51,250 KHR)
  Photo available: [SEND_PHOTO:0b8c6f0e-6a55-4c1f-9f3e-2d0c1b6a7e11]
• Krama — 4.00 USD (16,400 KHR)

PAYMENT QR
"""


def ask(said, system=SYSTEM):
    return generate_ai_reply({"system": system, "messages": [{"role": "user", "content": said}]})


def test_replies_without_a_key_and_says_it_is_a_demo(fake):
    reply = ask("how much?")
    assert reply.startswith(fake_ai.LABEL)
    assert "Silk Scarf — 12.50 USD" in reply


def test_quotes_the_product_the_customer_named(fake):
    reply = ask("how much is the krama?")
    assert "Krama — 4.00 USD (16,400 KHR)" in reply
    assert "Silk Scarf" not in reply


def test_a_khmer_message_gets_a_khmer_greeting(fake):
    assert "សួស្តី" in ask("តម្លៃប៉ុន្មាន?")


@pytest.mark.parametrize("said, marker", [
    ("can I pay by ABA?", PAYMENT_QR_MARKER),
    ("send me a photo", "[SEND_PHOTO:0b8c6f0e-6a55-4c1f-9f3e-2d0c1b6a7e11]"),
    ("can I get a discount", NEEDS_SELLER_MARKER),
])
def test_asks_for_the_same_markers_the_model_would(fake, said, marker):
    assert ask(said).splitlines()[-1] == marker


def test_never_offers_a_qr_the_shop_has_not_set_up(fake):
    assert PAYMENT_QR_MARKER not in ask("can I pay?", system="PRODUCT CATALOG\n• Krama — 4.00 USD")


def test_drafts_the_product_the_customer_named(fake):
    system = (
        "CATALOGUE\n"
        "- product=11111111-1111-1111-1111-111111111111 | Silk Scarf | stock=3\n"
        "- product=22222222-2222-2222-2222-222222222222 | "
        "variant=33333333-3333-3333-3333-333333333333 | Krama | options=Default | stock=9"
    )
    transcript = "customer: one krama please\nassistant: We have Silk Scarf too"
    draft = generate_order_draft({"system": system,
                                  "messages": [{"role": "user", "content": transcript}]})
    [item] = draft.items
    assert str(item.product_id) == "22222222-2222-2222-2222-222222222222"
    assert str(item.variant_id) == "33333333-3333-3333-3333-333333333333"
    assert item.quantity == 1


def test_reads_every_image_as_a_demo_receipt_keyed_to_the_image(fake):
    first = read_receipt(b"one image", "image/jpeg")
    again = read_receipt(b"one image", "image/jpeg")
    other = read_receipt(b"another image", "image/jpeg")

    assert (first.amount, first.currency) == (fake_ai.RECEIPT_AMOUNT, "USD")
    assert first.reference == again.reference != other.reference


def test_the_receipt_reading_comes_from_the_request_it_was_given(fake):
    request = build_receipt_messages(b"img", "image/png")
    assert fake_ai.receipt_reading(request)["reference"].startswith("DEMO-")


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_refused_outside_development(environment):
    settings = Settings(ENVIRONMENT=environment, AI_FAKE=True,
                        DATABASE_URL="postgresql://x/y", SECRET_KEY="s")
    with pytest.raises(RuntimeError, match="AI_FAKE"):
        settings.check_ai()


def test_allowed_in_development():
    Settings(ENVIRONMENT="development", AI_FAKE=True,
             DATABASE_URL="postgresql://x/y", SECRET_KEY="s").check_ai()
