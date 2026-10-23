"""Choosing which products the assistant sees, once a shop outgrows the prompt."""

from datetime import timedelta
from decimal import Decimal

from app.core.clock import utcnow
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services import catalogue
from app.services.catalogue import CATALOGUE_LIMIT
from tests import ai


def seller_id(client, seller):
    return client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]


def stock_shelves(db, user_id, count, *, prefix="Item"):
    """`count` plain products, newest last."""
    now = utcnow()
    for i in range(count):
        db.add(Product(user_id=user_id, name=f"{prefix} {i:03d}", price=Decimal("5.00"),
                       stock=3, is_active=True, created_at=now - timedelta(minutes=count - i)))
    db.commit()


def old_product(db, user_id, name, *, sku=None, description=None):
    """A product older than everything else, so recency alone never picks it."""
    product = Product(user_id=user_id, name=name, price=Decimal("7.25"), stock=4,
                      description=description, is_active=True,
                      created_at=utcnow() - timedelta(days=60))
    db.add(product)
    db.flush()
    if sku:
        db.add(ProductVariant(user_id=user_id, product_id=product.id, option_values={},
                              option_signature="", sku=sku, price=Decimal("7.25"),
                              stock=4, is_active=True, is_default=True))
    db.commit()
    return product


def ask(client, seller, conversation, text):
    with ai.replies("Yes.") as captured:
        r = client.post(f"/api/v1/chat/{conversation['id']}",
                        json={"message": text, "message_type": "text"},
                        headers=seller.headers)
    assert r.status_code == 200, r.text
    return captured.system_prompt


def test_a_shop_that_fits_is_shown_whole_with_no_index(client, seller):
    seller.product(name="Silk Scarf")
    prompt = ask(client, seller, seller.conversation(), "hello")
    assert "Silk Scarf" in prompt
    assert "This shop has" not in prompt


def test_an_older_product_the_customer_names_is_shown_in_full(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 30)
    old_product(db, uid, "Silk Krama")

    prompt = ask(client, seller, seller.conversation(), "Do you still have the silk krama?")

    assert "• Silk Krama — 7.25 USD" in prompt
    assert f"This shop has {CATALOGUE_LIMIT + 31} products" in prompt


def test_a_khmer_name_inside_a_sentence_is_found(client, seller, db):
    """Khmer has no spaces between words, so the name is not a token of its own."""
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    old_product(db, uid, "ក្រមាសូត្រ")

    prompt = ask(client, seller, seller.conversation(), "តើមានក្រមាសូត្រទេបង?")

    assert "• ក្រមាសូត្រ — 7.25 USD" in prompt


def test_a_product_named_earlier_in_the_thread_stays_in_view(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    old_product(db, uid, "Silk Krama")
    conversation = seller.conversation()

    ask(client, seller, conversation, "Do you have silk krama?")
    prompt = ask(client, seller, conversation, "how much for two?")

    assert "Silk Krama" in prompt.split("PRODUCT CATALOG")[1].split("This shop has")[0]


def test_a_typed_sku_finds_its_product(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    old_product(db, uid, "Hand-woven bag", sku="BAG-RED-01")

    prompt = ask(client, seller, seller.conversation(), "price of bag-red-01 please")

    assert "Hand-woven bag" in prompt.split("This shop has")[0]


def test_a_word_in_the_description_counts(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    old_product(db, uid, "Model K", description="Pure Cambodian silk, from Takeo")

    prompt = ask(client, seller, seller.conversation(), "anything from takeo?")

    assert "• Model K" in prompt.split("This shop has")[0]


def test_unmatched_products_are_listed_by_name_and_never_priced(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 3)

    prompt = ask(client, seller, seller.conversation(), "hi")

    # The three oldest fall out of the full listing and into the index.
    index = prompt.split("This shop has")[1]
    for name in ("Item 000", "Item 001", "Item 002"):
        assert f"• {name}\n" in index
        assert f"{name} —" not in prompt
    assert "do not guess its price or stock" in index


def test_best_sellers_fill_the_listing_before_newer_products(client, seller, db):
    uid = seller_id(client, seller)
    best = seller.product(name="Bestseller Sarong", price="9.00", stock=50)
    db.query(Product).filter(Product.id == best["id"]).update(
        {Product.created_at: utcnow() - timedelta(days=30)})
    db.commit()
    customer = seller.customer()
    assert seller.order(customer["id"], [{"product_id": best["id"], "quantity": 3}]).status_code == 201
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)

    selection = catalogue.select_products(db, uid, [])

    assert selection.products[0].name == "Bestseller Sarong"


def test_the_index_is_bounded(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + catalogue.NAME_INDEX_LIMIT + 7)

    selection = catalogue.select_products(db, uid, [])
    section = catalogue.name_index_section(selection)

    assert len(selection.products) == CATALOGUE_LIMIT
    assert len(selection.other_names) == catalogue.NAME_INDEX_LIMIT
    assert "…and 7 more." in section


def test_order_drafts_see_the_product_the_customer_named(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    seller.product(name="Silk Krama", price="7.25", stock=4)
    db.query(Product).filter(Product.name == "Silk Krama").update(
        {Product.created_at: utcnow() - timedelta(days=60)})
    db.commit()
    conversation = seller.conversation()
    ask(client, seller, conversation, "I'll take one silk krama")

    with ai.replies('{"items": [], "delivery_address": null, "notes": null}') as captured:
        client.post(f"/api/v1/chat/{conversation['id']}/order-draft", headers=seller.headers)

    assert "Silk Krama" in captured.system_prompt


def test_like_wildcards_in_a_message_are_literal(client, seller, db):
    uid = seller_id(client, seller)
    stock_shelves(db, uid, CATALOGUE_LIMIT + 5)
    # Unescaped, "a_b" would match "aXb" as a LIKE pattern and pull this in.
    old_product(db, uid, "Mug aXb")
    said = type("M", (), {"content": "a_b", "sender_type": "customer"})()

    selection = catalogue.select_products(db, uid, [said])

    assert "Mug aXb" not in [p.name for p in selection.products]
