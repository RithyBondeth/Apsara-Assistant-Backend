"""Taking card payments: creating a checkout, and believing a payment happened."""

from decimal import Decimal

import pytest

from app.core import crypto
from app.models.order import Order
from app.models.platform_connection import PlatformConnection
from app.services import stripe_gateway
from tests.conftest import register


def connect_stripe(client, seller, *, account="acct_test_1", secret="whsec_test"):
    r = client.post("/api/v1/integrations/",
                    json={"platform": "stripe", "external_id": account,
                          "access_token": "sk_test_key", "webhook_secret": secret,
                          "display_name": "Sok Silk Shop"},
                    headers=seller.headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_order(seller, price="20.00", qty=2):
    product = seller.product(price=price, stock=10)
    customer = seller.customer()
    r = seller.order(customer["id"], [{"product_id": product["id"], "quantity": qty}])
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def fake_session(monkeypatch):
    """Stand in for Stripe, recording what it was asked to charge."""
    calls = []

    def create(encrypted_key, **kwargs):
        calls.append(kwargs)
        return stripe_gateway.CheckoutSession(
            id="cs_test_123", url="https://checkout.stripe.com/c/pay/cs_test_123")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", create)
    return calls


def stripe_event(order_id, session_id="cs_test_123", kind="checkout.session.completed"):
    return {"type": kind,
            "data": {"object": {"id": session_id,
                                "metadata": {"order_id": str(order_id)}}}}


@pytest.fixture
def accept_signature(monkeypatch):
    """Treat the delivery as genuinely Stripe's, without a real signature."""
    def verify(payload, signature, encrypted_secret):
        return verify.event

    verify.event = None
    monkeypatch.setattr(stripe_gateway, "verify_webhook", verify)
    return verify


# ── Amounts ──────────────────────────────────────────────────────────────────

def test_dollar_amounts_convert_to_cents():
    assert stripe_gateway.to_minor_units(Decimal("20.00"), "USD") == 2000
    assert stripe_gateway.to_minor_units(Decimal("12.34"), "USD") == 1234


def test_riel_is_not_treated_as_zero_decimal():
    """This app writes riel whole (៛50,000), but Stripe does not list KHR as a
    zero-decimal currency. Following the display convention here would charge a
    hundredth of the intended amount."""
    assert stripe_gateway.to_minor_units(Decimal("50000"), "KHR") == 5000000


def test_genuinely_zero_decimal_currencies_are_sent_whole():
    assert stripe_gateway.to_minor_units(Decimal("500"), "JPY") == 500


# ── Creating a checkout ──────────────────────────────────────────────────────

def test_checkout_returns_a_payment_link(client, seller, fake_session):
    connect_stripe(client, seller)
    order = make_order(seller)

    r = client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)

    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    assert r.json()["payment_status"] == "pending"


def test_checkout_charges_the_order_total(client, seller, fake_session):
    connect_stripe(client, seller)
    order = make_order(seller, price="20.00", qty=2)

    client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)

    assert fake_session[0]["amount"] == Decimal("40.00")
    assert fake_session[0]["currency"] == "USD"
    # The webhook has nothing else to match on, so this has to travel with it.
    assert str(fake_session[0]["order_id"]) == order["id"]


def test_checkout_needs_stripe_connected(client, seller, fake_session):
    order = make_order(seller)

    r = client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)

    assert r.status_code == 400
    assert "Integrations" in r.json()["detail"]


def test_checkout_is_refused_for_a_cancelled_order(client, seller, fake_session):
    connect_stripe(client, seller)
    order = make_order(seller)
    client.patch(f"/api/v1/orders/{order['id']}", json={"status": "cancelled"},
                 headers=seller.headers)

    r = client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)

    assert r.status_code == 400


def test_one_seller_cannot_bill_anothers_order(client, seller, other_seller, fake_session):
    connect_stripe(client, other_seller, account="acct_test_2")
    order = make_order(seller)

    r = client.post(f"/api/v1/orders/{order['id']}/checkout",
                    headers=other_seller.headers)

    assert r.status_code == 404


def test_reissuing_replaces_the_session(client, seller, fake_session):
    """Checkout Sessions expire after a day; a slow customer should not need
    the order rebuilt."""
    connect_stripe(client, seller)
    order = make_order(seller)

    first = client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)
    second = client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)

    assert first.status_code == 200 and second.status_code == 200
    assert len(fake_session) == 2


# ── Confirming payment ───────────────────────────────────────────────────────

def post_event(client, connection_id, body):
    return client.post(f"/api/v1/webhooks/stripe/{connection_id}", json=body,
                       headers={"Stripe-Signature": "t=1,v1=whatever"})


def test_a_signed_completion_marks_the_order_paid(client, seller, db,
                                                  fake_session, accept_signature):
    connection = connect_stripe(client, seller)
    order = make_order(seller)
    client.post(f"/api/v1/orders/{order['id']}/checkout", headers=seller.headers)
    accept_signature.event = stripe_event(order["id"])

    r = post_event(client, connection["id"], accept_signature.event)

    assert r.status_code == 200
    row = db.query(Order).filter(Order.id == order["id"]).first()
    assert row.payment_status == "paid"
    assert row.payment_method == "stripe"
    assert row.paid_at is not None


def test_an_unverified_delivery_is_refused(client, seller, db, fake_session, monkeypatch):
    """Anyone can POST at a public URL. Without the signature check this
    endpoint would hand out paid orders for free."""
    connection = connect_stripe(client, seller)
    order = make_order(seller)
    monkeypatch.setattr(stripe_gateway, "verify_webhook", lambda *a, **k: None)

    r = post_event(client, connection["id"], stripe_event(order["id"]))

    assert r.status_code == 403
    assert db.query(Order).filter(Order.id == order["id"]).first().payment_status == "unpaid"


def test_redelivery_does_not_move_the_paid_time(client, seller, db,
                                                fake_session, accept_signature):
    """Stripe retries whenever it is unsure a delivery arrived, so this runs
    more than once for one payment."""
    connection = connect_stripe(client, seller)
    order = make_order(seller)
    accept_signature.event = stripe_event(order["id"])

    post_event(client, connection["id"], accept_signature.event)
    first_paid_at = db.query(Order).filter(Order.id == order["id"]).first().paid_at
    db.expire_all()
    post_event(client, connection["id"], accept_signature.event)

    row = db.query(Order).filter(Order.id == order["id"]).first()
    assert row.paid_at == first_paid_at


def test_an_event_cannot_pay_another_sellers_order(client, seller, other_seller, db,
                                                   fake_session, accept_signature):
    """The order is looked up within the connection's owner, so quoting a
    stranger's order id in metadata achieves nothing."""
    connection = connect_stripe(client, other_seller, account="acct_test_2")
    victim_order = make_order(seller)
    accept_signature.event = stripe_event(victim_order["id"])

    r = post_event(client, connection["id"], accept_signature.event)

    assert r.status_code == 200  # acknowledged, so Stripe stops retrying
    row = db.query(Order).filter(Order.id == victim_order["id"]).first()
    assert row.payment_status == "unpaid"


def test_other_event_types_are_acknowledged_and_ignored(client, seller, db,
                                                        fake_session, accept_signature):
    connection = connect_stripe(client, seller)
    order = make_order(seller)
    accept_signature.event = stripe_event(order["id"], kind="payment_intent.created")

    r = post_event(client, connection["id"], accept_signature.event)

    assert r.status_code == 200
    assert db.query(Order).filter(Order.id == order["id"]).first().payment_status == "unpaid"


def test_an_unknown_connection_is_refused(client, seller, accept_signature):
    import uuid

    accept_signature.event = stripe_event(uuid.uuid4())
    assert post_event(client, uuid.uuid4(), accept_signature.event).status_code == 403


# ── How the credentials are held ─────────────────────────────────────────────

def test_stripe_needs_a_signing_secret_to_connect(client, seller):
    """Without it no payment notification can be authenticated, so refuse at
    connect time rather than on the first real payment."""
    r = client.post("/api/v1/integrations/",
                    json={"platform": "stripe", "external_id": "acct_x",
                          "access_token": "sk_test_key"},
                    headers=seller.headers)

    assert r.status_code == 400


def test_neither_stripe_secret_reads_back_out(client, seller, db):
    connection = connect_stripe(client, seller)

    assert connection["webhook_secret"] is None
    listed = client.get("/api/v1/integrations/", headers=seller.headers).json()[0]
    assert listed["webhook_secret"] is None
    assert "access_token" not in listed


def test_both_stripe_secrets_are_encrypted_at_rest(client, seller, db):
    connect_stripe(client, seller, secret="whsec_supersecret")
    row = db.query(PlatformConnection).filter(
        PlatformConnection.platform == "stripe").first()

    assert row.access_token != "sk_test_key"
    assert row.webhook_secret != "whsec_supersecret"
    assert crypto.decrypt(row.access_token) == "sk_test_key"
    assert crypto.decrypt(row.webhook_secret) == "whsec_supersecret"
