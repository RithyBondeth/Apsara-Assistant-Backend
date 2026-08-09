"""The shop's currency, and how it reaches orders and the assistant."""

import pytest

from app.core.currency import format_amount
from tests import ai


def set_currency(client, seller, currency):
    return client.patch("/api/v1/auth/me", json={"currency": currency},
                        headers=seller.headers)


# ── Formatting ───────────────────────────────────────────────────────────────

def test_dollars_carry_cents():
    assert format_amount(12.5, "USD") == "12.50 USD"


def test_riel_is_quoted_whole():
    """Nobody writes ៛50,000.00."""
    assert format_amount(50000, "KHR") == "50,000 KHR"


# ── The seller's setting ─────────────────────────────────────────────────────

def test_shops_default_to_dollars(client, seller):
    me = client.get("/api/v1/auth/me", headers=seller.headers)
    assert me.json()["currency"] == "USD"


def test_seller_can_switch_currency(client, seller):
    assert set_currency(client, seller, "KHR").status_code == 200
    assert client.get("/api/v1/auth/me",
                      headers=seller.headers).json()["currency"] == "KHR"


@pytest.mark.parametrize("bad", ["usd", "GBP", "$", "", "DOLLARS"])
def test_unsupported_currencies_are_refused(client, seller, bad):
    """An open text column would let 'usd', 'Dollars' and '$' all coexist."""
    assert set_currency(client, seller, bad).status_code == 422


# ── What the assistant is told ───────────────────────────────────────────────

def test_catalogue_states_the_currency(client, seller):
    seller.product("Silk Scarf", "12.50", 8)
    conversation = seller.conversation()

    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=seller.headers)

    assert "• Silk Scarf — 12.50 USD" in captured.system_prompt
    assert "Prices are in USD" in captured.system_prompt


def test_switching_currency_changes_what_the_assistant_quotes(client, seller):
    seller.product("Krama", "50000", 8)
    set_currency(client, seller, "KHR")
    conversation = seller.conversation()

    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=seller.headers)

    assert "• Krama — 50,000 KHR" in captured.system_prompt
    assert "Prices are in KHR" in captured.system_prompt
    assert "12.50 USD" not in captured.system_prompt


def test_the_model_is_told_not_to_convert(client, seller):
    conversation = seller.conversation()
    with ai.replies() as captured:
        client.post(f"/api/v1/chat/{conversation['id']}", json={"message": "hi"},
                    headers=seller.headers)
    assert "never convert to another currency" in captured.system_prompt


# ── Orders ───────────────────────────────────────────────────────────────────

def test_order_records_the_shops_currency(client, seller):
    set_currency(client, seller, "KHR")
    product = seller.product(price="50000", stock=5)
    customer = seller.customer()

    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 2}]).json()

    assert order["currency"] == "KHR"
    assert order["total_amount"] == "100000.00"


def test_switching_currency_does_not_reprice_past_orders(client, seller):
    """The order keeps the currency it was actually agreed in."""
    product = seller.product(price="12.50", stock=5)
    customer = seller.customer()
    order = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 1}]).json()
    assert order["currency"] == "USD"

    set_currency(client, seller, "KHR")

    after = client.get(f"/api/v1/orders/{order['id']}", headers=seller.headers).json()
    assert after["currency"] == "USD"
    # A new order takes the new currency.
    fresh = seller.order(customer["id"],
                         [{"product_id": product["id"], "quantity": 1}]).json()
    assert fresh["currency"] == "KHR"
