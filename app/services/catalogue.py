"""Which products the assistant sees in full.

A shop's whole catalogue goes into the prompt while it fits — CATALOGUE_LIMIT
products. Past that, dumping the newest hundred meant a customer asking about
an older product was told it did not exist. Instead, the products this
conversation is about come first, then the shop's best sellers, and every
other product is listed by name so the assistant knows it exists.

Relevance is plain text matching, in Postgres, against the recent thread:
- a product whose name appears anywhere in what was said. This is the match
  that works for Khmer, which is written without spaces between words, so a
  name inside a sentence is not a separate token;
- words of the latest customer messages found in a name or description;
- a variant SKU typed by the customer.

No embeddings or extensions: it works on any Postgres the shop is given, and a
customer naming the product is overwhelmingly how they ask about one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import and_, case, exists, func, literal, select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import utcnow
from app.models.message import Message
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.product_variant import ProductVariant

# Products shown in full. Shared with ai_service, which renders them.
CATALOGUE_LIMIT = 100
# Names of the remaining products, so the model knows they exist.
NAME_INDEX_LIMIT = 300
# Customer messages whose words are searched, newest first.
QUERY_MESSAGES = 3
MAX_TOKENS = 12
SOLD_WINDOW = timedelta(days=90)

# Split on spaces and punctuation only. \W would also split Khmer words at
# their vowel signs, which Unicode does not count as word characters.
_SEPARATORS = re.compile(r"[\s,.!?;:()\[\]{}\"'/\\|<>*+=~`@#$%^&·។៕]+")


@dataclass
class Selection:
    products: list[Product]
    total: int
    # Names of active products not in `products`, best sellers first.
    other_names: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.total > len(self.products)


def _tokens(texts: list[str]) -> list[str]:
    seen: list[str] = []
    for text in texts:
        for token in _SEPARATORS.split(text.lower()):
            if len(token) >= 2 and token not in seen:
                seen.append(token)
            if len(seen) >= MAX_TOKENS:
                return seen
    return seen


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _active(db: Session, user_id):
    return db.query(Product).filter(Product.user_id == user_id, Product.is_active == True)  # noqa: E712


def select_products(db: Session, user_id, history: list[Message]) -> Selection:
    """The products to render for a reply to `history` (oldest first)."""
    total = _active(db, user_id).count()
    if total <= CATALOGUE_LIMIT:
        products = (_active(db, user_id).options(selectinload(Product.variants))
                    .order_by(Product.created_at.desc()).all())
        return Selection(products=products, total=total)

    said = " ".join(m.content for m in history if m.content).lower()[-6000:]
    latest = [m.content for m in reversed(history)
              if m.sender_type == "customer" and m.content][:QUERY_MESSAGES]
    tokens = _tokens(latest)

    name = func.lower(Product.name)
    score = case((and_(func.length(Product.name) >= 2,
                       func.strpos(literal(said), name) > 0), 10), else_=0)
    for token in tokens:
        pattern = _like(token)
        score = score + case((Product.name.ilike(pattern, escape="\\"), 3), else_=0)
        score = score + case((Product.description.ilike(pattern, escape="\\"), 1), else_=0)
    if tokens:
        sku_hit = exists().where(
            ProductVariant.product_id == Product.id,
            ProductVariant.is_active == True,  # noqa: E712
            func.lower(ProductVariant.sku).in_(tokens),
        )
        score = score + case((sku_hit, 8), else_=0)

    sold = (
        select(OrderItem.product_id, func.sum(OrderItem.quantity).label("sold"))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user_id, Order.status != "cancelled",
               Order.created_at >= utcnow() - SOLD_WINDOW)
        .group_by(OrderItem.product_id)
        .subquery()
    )
    ranked = (
        db.query(Product.id, Product.name)
        .outerjoin(sold, sold.c.product_id == Product.id)
        .filter(Product.user_id == user_id, Product.is_active == True)  # noqa: E712
        .order_by(score.desc(), func.coalesce(sold.c.sold, 0).desc(),
                  Product.created_at.desc(), Product.id)
        .limit(CATALOGUE_LIMIT + NAME_INDEX_LIMIT)
        .all()
    )
    chosen_ids = [row.id for row in ranked[:CATALOGUE_LIMIT]]
    by_id = {p.id: p for p in (_active(db, user_id).options(selectinload(Product.variants))
                               .filter(Product.id.in_(chosen_ids)).all())}
    return Selection(
        products=[by_id[i] for i in chosen_ids if i in by_id],
        total=total,
        other_names=[row.name for row in ranked[CATALOGUE_LIMIT:]],
    )


def name_index_section(selection: Selection) -> str:
    """Prompt text for the products not shown in full; empty when all are."""
    if not selection.partial:
        return ""
    names = "\n".join(f"• {name}" for name in selection.other_names)
    hidden = selection.total - len(selection.products) - len(selection.other_names)
    more = f"\n…and {hidden} more." if hidden > 0 else ""
    return f"""
This shop has {selection.total} products. The {len(selection.products)} listed above
are the ones most relevant to this conversation, in full. These others also
exist, but you do not have their prices or stock:
{names}{more}
If the customer seems to mean one of these, do not guess its price or stock:
ask them to write its name so you can look it up for them.
"""
