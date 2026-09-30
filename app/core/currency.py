"""The currencies a shop can price in.

Cambodian sellers quote in US dollars, Khmer riel, or both. The set is small on
purpose — an open text column would let "usd", "Dollars" and "$" all mean the
same thing — and adding to it is a one-line change plus a display rule.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

TCurrency = Literal["USD", "KHR"]

CURRENCIES: tuple[str, ...] = ("USD", "KHR")

DEFAULT_CURRENCY = "USD"

# Riel is quoted in whole units — nobody writes ៛50,000.00.
DECIMALS: dict[str, int] = {"USD": 2, "KHR": 0}


def convert(amount, from_currency: str, to_currency: str, khr_rate) -> Decimal:
    """Move an amount between the two currencies at the shop's rate."""
    amount = Decimal(amount)
    if from_currency == to_currency:
        return amount
    rate = Decimal(khr_rate)
    if from_currency == "USD" and to_currency == "KHR":
        return (amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if from_currency == "KHR" and to_currency == "USD":
        return (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raise ValueError(f"No conversion from {from_currency} to {to_currency}")


def other_currency(currency: str) -> str:
    return "KHR" if currency == "USD" else "USD"


def format_dual(amount, currency: str, khr_rate) -> str:
    """Both currencies, the way a shop quotes: "8.00 USD (32,800 KHR)".

    The catalogue currency leads; the other is what the customer may pay in.
    """
    other = other_currency(currency)
    return (f"{format_amount(amount, currency)} "
            f"({format_amount(convert(amount, currency, other, khr_rate), other)})")


def format_amount(amount, currency: str) -> str:
    """Render an amount the way the seller would write it.

    Used for the assistant's catalogue, where the currency has to be explicit:
    quoting a bare number leaves the model to guess, and guessing wrong about
    money is the one mistake a shop cannot absorb.
    """
    places = DECIMALS.get(currency, 2)
    return f"{amount:,.{places}f} {currency}"
