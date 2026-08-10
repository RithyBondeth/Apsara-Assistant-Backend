"""Card payments through Stripe Checkout.

Each seller connects their own Stripe account, and every call here is made with
that seller's key — so the money moves from the customer to the seller without
passing through this platform. That keeps the app out of the business of
holding other people's funds, which is a licensing question rather than an
engineering one.

Checkout is hosted by Stripe. The card form lives on Stripe's domain, so no
card number ever reaches this server; what comes back is a URL to send the
customer, which suits a shop whose whole conversation happens inside Messenger
or Telegram.

Payment is confirmed by webhook, never by the customer returning to a success
page: a closed tab, a dropped connection or a bookmarked success URL would
otherwise decide whether a seller gets paid.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import stripe

from app.core.crypto import decrypt
from app.services.platforms import CheckResult

logger = logging.getLogger(__name__)

STRIPE = "stripe"

TIMEOUT = 20

# Stripe expects amounts in the currency's smallest unit — cents for USD. For a
# handful of currencies there is no smaller unit, and the amount is sent whole.
#
# This deliberately does NOT reuse app.core.currency.DECIMALS. That map says how
# a seller writes an amount (riel is quoted whole: ៛50,000), which is a display
# rule. Stripe has its own opinion per currency, and KHR is not on its
# zero-decimal list — so a riel amount goes to Stripe multiplied by 100 even
# though nobody writes riel that way. Conflating the two would charge every
# riel customer a hundred times the intended amount.
STRIPE_ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})


def to_minor_units(amount: Decimal, currency: str) -> int:
    """Convert a stored decimal amount to the integer Stripe expects."""
    if currency.upper() in STRIPE_ZERO_DECIMAL:
        return int(Decimal(amount).to_integral_value())
    return int((Decimal(amount) * 100).to_integral_value())


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


def _client(encrypted_key: str) -> stripe.StripeClient:
    """A Stripe client bound to one seller's key.

    A client per call rather than the module-level `stripe.api_key`: that global
    is shared by every request in the process, so under any concurrency one
    seller's checkout could be created against another seller's account.

    If this ever moves to Stripe Connect, this is the seam — the platform key
    would go here and the seller's account id into `stripe_account=`, leaving
    every caller unchanged.
    """
    return stripe.StripeClient(decrypt(encrypted_key))


# ── Connecting an account ────────────────────────────────────────────────────

def check_credentials(encrypted_key: str) -> CheckResult:
    """Ask Stripe whether this key works, and whose account it belongs to."""
    try:
        key = decrypt(encrypted_key)
    except Exception:
        return CheckResult(False, "Stored Stripe key could not be read. "
                                  "Reconnect Stripe to store it again.")
    try:
        account = stripe.Account.retrieve(api_key=key, timeout=TIMEOUT)
    except stripe.AuthenticationError:
        return CheckResult(False, "Stripe rejected that key. Check you copied "
                                  "the whole secret key.")
    except stripe.PermissionError:
        return CheckResult(False, "That key is missing permissions. A restricted "
                                  "key needs write access to Checkout Sessions.")
    except stripe.APIConnectionError:
        return CheckResult(False, "Could not reach Stripe. Try again shortly.")
    except stripe.StripeError as exc:
        logger.warning("Stripe credential check failed: %s", exc)
        return CheckResult(False, "Stripe refused the request.")
    except Exception:
        logger.exception("Stripe credential check failed unexpectedly")
        return CheckResult(False, "The check failed unexpectedly.")

    name = (account.get("business_profile") or {}).get("name") or account.get("id")
    if not account.get("charges_enabled"):
        # A real and common state: the account exists but Stripe has not
        # finished onboarding it, so a checkout would be created and then fail
        # at the moment a customer tries to pay.
        return CheckResult(False, f"Connected to {name}, but this Stripe account "
                                  "cannot accept charges yet. Finish onboarding "
                                  "in the Stripe dashboard.")
    return CheckResult(True, f"Connected to {name}.")


# ── Taking a payment ─────────────────────────────────────────────────────────

def create_checkout_session(
    encrypted_key: str,
    *,
    order_id,
    amount: Decimal,
    currency: str,
    description: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> CheckoutSession:
    """Open a hosted payment page for one order.

    The whole order is charged as a single line rather than itemised. Stripe
    would happily take the line items, but they would then be a second copy of
    something this database already owns, free to disagree with it the moment an
    order is edited — and the customer's card is charged from the total either
    way.
    """
    client = _client(encrypted_key)
    session = client.checkout.sessions.create(
        params={
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "customer_email": customer_email,
            "line_items": [{
                "quantity": 1,
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": to_minor_units(amount, currency),
                    "product_data": {"name": description},
                },
            }],
            # The order id travels with the payment so the webhook can find it
            # again. Stripe echoes metadata back on every event for this
            # session, which is what makes the confirmation self-contained.
            "metadata": {"order_id": str(order_id)},
        },
        options={"timeout": TIMEOUT},
    )
    return CheckoutSession(id=session.id, url=session.url)


def verify_webhook(payload: bytes, signature: str | None, encrypted_secret: str) -> dict | None:
    """Authenticate a Stripe webhook and return its event.

    Returns None when the delivery cannot be proven to be Stripe's. Anyone can
    POST an invoice-paid event at a public URL; without this check the endpoint
    would mark orders paid for free.
    """
    if not signature:
        return None
    try:
        secret = decrypt(encrypted_secret)
    except Exception:
        logger.error("Stored Stripe webhook secret could not be decrypted")
        return None

    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except (ValueError, stripe.SignatureVerificationError):
        # Covers a forged signature and a replay outside Stripe's tolerance.
        logger.warning("Rejected a Stripe webhook with an invalid signature")
        return None
    return dict(event)
