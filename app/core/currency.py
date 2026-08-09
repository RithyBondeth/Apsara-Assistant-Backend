"""The currencies a shop can price in.

Cambodian sellers quote in US dollars, Khmer riel, or both. The set is small on
purpose — an open text column would let "usd", "Dollars" and "$" all mean the
same thing — and adding to it is a one-line change plus a display rule.
"""

from typing import Literal

TCurrency = Literal["USD", "KHR"]

CURRENCIES: tuple[str, ...] = ("USD", "KHR")

DEFAULT_CURRENCY = "USD"

# Riel is quoted in whole units — nobody writes ៛50,000.00.
DECIMALS: dict[str, int] = {"USD": 2, "KHR": 0}


def format_amount(amount, currency: str) -> str:
    """Render an amount the way the seller would write it.

    Used for the assistant's catalogue, where the currency has to be explicit:
    quoting a bare number leaves the model to guess, and guessing wrong about
    money is the one mistake a shop cannot absorb.
    """
    places = DECIMALS.get(currency, 2)
    return f"{amount:,.{places}f} {currency}"
