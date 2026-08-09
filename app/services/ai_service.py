from __future__ import annotations

import logging
from functools import lru_cache

from openai import OpenAI, OpenAIError

from app.core.config import settings
from app.models.message import Message
from app.models.product import Product
from app.models.user import User

logger = logging.getLogger(__name__)

# Messages of history sent to OpenAI. Bounded so a long-running conversation
# does not grow the prompt without limit, but wide enough that follow-ups which
# lean on earlier turns ("how much for two?") still resolve.
HISTORY_LIMIT = 20

# Catalogue rows in the prompt. A seller past this many products needs retrieval
# rather than a full dump; until then the whole catalogue fits comfortably.
CATALOGUE_LIMIT = 100

# A message the seller typed by hand is, to the customer, the same voice as the
# assistant's — the thread reads as one shop replying, so the model sees it that
# way too.
ROLE_BY_SENDER = {"customer": "user", "assistant": "assistant", "seller": "assistant"}


class AIError(RuntimeError):
    """Raised when a reply could not be generated."""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    """The shared OpenAI client.

    Built once and reused so replies do not pay for a fresh connection pool on
    every message. Constructed lazily rather than at import so the app still
    starts without a key configured.
    """
    return OpenAI(api_key=settings.OPENAI_API_KEY)


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(user: User, products: list[Product]) -> str:
    business = user.business_name or user.full_name

    product_lines: list[str] = []
    for p in products[:CATALOGUE_LIMIT]:
        line = f"• {p.name} — {p.price:,.2f}"
        if p.description:
            line += f"\n  {p.description}"
        line += f"\n  {'OUT OF STOCK' if not p.stock else f'Stock: {p.stock} units'}"
        product_lines.append(line)

    product_catalog = "\n\n".join(product_lines) or "No products are listed yet."

    return f"""You are Apsara, an AI-powered sales assistant for "{business}".

Your role:
- Answer questions about products (price, availability, description)
- Help customers pick the right item
- Assist with order placement
- Be friendly, warm, and concise — like a real shop owner chatting on Messenger or Telegram

━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRODUCT CATALOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━
{product_catalog}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply in the same language and script the customer wrote in, deciding per
message from the message itself:
- Khmer script (ខ្មែរ) in — Khmer script out.
- English in — English out.
- Romanized Khmer in (Khmer words typed in Latin letters, e.g. "tlai ponman")
  — romanized Khmer out, short and casual like a shop owner on Messenger. Do
  not convert it to Khmer script or answer in English.
- If the customer mixes languages, mirror the mix.
Never switch the customer to a different language than the one they chose.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
BEHAVIOR RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Give prices directly — never make a customer ask twice
- Quote prices exactly as the numbers appear in the catalog above; do not
  attach a currency symbol that is not written there
- If an item is out of stock, apologize and suggest alternatives if available
- Keep replies short (2–4 sentences max unless more detail is truly needed)
- Never invent products, prices, stock levels, discounts or delivery dates
- You cannot confirm an order yourself. When a customer wants to buy, collect
  their name, phone number and delivery address, then say the seller will
  confirm shortly
- If you cannot answer, politely say you will check with the seller
"""


# ── Conversation history builder ──────────────────────────────────────────────

def build_openai_messages(system_prompt: str, history: list[Message]) -> list[dict]:
    """Assemble the request payload.

    `history` is in chronological order and already ends with the customer
    message being answered.
    """
    turns = [
        {"role": ROLE_BY_SENDER[m.sender_type], "content": m.content}
        for m in history
        if m.content and m.sender_type in ROLE_BY_SENDER
    ]
    return [{"role": "system", "content": system_prompt}, *turns]


# ── OpenAI call ───────────────────────────────────────────────────────────────

def generate_ai_reply(openai_messages: list[dict]) -> str:
    """Call the model and return its reply. Raises `AIError` on any failure.

    Synchronous on purpose: the rest of the app uses a sync SQLAlchemy session,
    so the endpoint is a `def` that FastAPI runs in its threadpool. Awaiting
    here inside an `async def` would put blocking DB calls on the event loop.
    """
    if not settings.OPENAI_API_KEY:
        raise AIError("AI replies are not configured. Set OPENAI_API_KEY on the server.")

    try:
        response = _client().chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=openai_messages,
            temperature=0.65,
            max_tokens=400,
        )
    except OpenAIError as exc:
        # Logged in full for operators; the caller gets a generic message so
        # upstream error text never reaches an API client.
        logger.exception("OpenAI request failed")
        raise AIError("The AI assistant is unavailable right now. Please try again.") from exc

    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        raise AIError("The AI assistant returned an empty reply. Please try again.")
    return reply
