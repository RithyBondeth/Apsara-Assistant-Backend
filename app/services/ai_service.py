from __future__ import annotations

import re

from openai import AsyncOpenAI

from app.core.config import settings
from app.models.message import Message
from app.models.product import Product
from app.models.user import User

# ── Khmer language detection ──────────────────────────────────────────────────

# Unicode block U+1780–U+17FF covers Khmer script
_KHMER_UNICODE_RANGE = range(0x1780, 0x1800)

# Common romanized Khmer words/patterns that hint at phonetic Khmer
_ROMANIZED_KHMER_HINTS = {
    "thlai", "ponman", "mean", "ot tov", "ot", "baat", "bat",
    "orkoun", "arkoun", "som", "chhnoul", "khnhom", "anak",
    "ning", "dael", "min", "chea", "nih", "nuh", "ho",
    "lok", "bong", "oun", "neang", "pros", "srey",
    "sok sabay", "sabay", "riab", "chuon", "chum reab",
}

# Whole words only. These hints are short, and a plain substring test made
# ordinary English read as Khmer — "ho" matched "how", "ot" matched "not",
# "photo" and "lot", "som" matched "some", "ning" matched "morning". "How much
# is this?" was answered in Khmer.
_ROMANIZED_KHMER_RE = re.compile(
    r"\b(?:"
    + "|".join(sorted((re.escape(h) for h in _ROMANIZED_KHMER_HINTS), key=len, reverse=True))
    + r")\b"
)

# Hints that are also ordinary English words. One on its own proves nothing
# ("what do you mean?"), so it only counts alongside another hint.
_ALSO_ENGLISH_WORDS = {"mean", "bat", "min", "pros", "bong", "ho", "lok", "som"}


def detect_language(text: str) -> str:
    """
    Detect whether the message is:
      - 'khmer'           — contains Khmer Unicode characters
      - 'romanized_khmer' — phonetic Khmer written in Latin script
      - 'english'         — standard English

    Romanized Khmer is inherently fuzzy — it has no standard spelling and
    borrows the Latin alphabet — so this leans toward 'english' when the only
    evidence is a word English also uses. Guessing Khmer wrongly is the worse
    error: it answers an English speaker in a script they may not read.
    """
    if any(ord(c) in _KHMER_UNICODE_RANGE for c in text):
        return "khmer"

    hits = {m.group(0) for m in _ROMANIZED_KHMER_RE.finditer(text.lower())}
    unambiguous = hits - _ALSO_ENGLISH_WORDS

    if unambiguous or len(hits) >= 2:
        return "romanized_khmer"

    return "english"


def normalize_message(text: str) -> tuple[str, str]:
    """
    Returns (normalized_text, detected_language).
    Currently passes text through unchanged; the language hint is used
    in the system prompt so the AI knows how to reply.
    """
    lang = detect_language(text)
    return text.strip(), lang


def detect_reply_language(text: str, history: list[Message]) -> str:
    """Language to reply in, tolerating a message that carries no text.

    A customer who sends only a photo gives no language signal, and
    ``detect_language("")`` falls through to English — which would answer a
    Khmer speaker in English on the one message type where they said nothing
    wrong. Fall back to the last thing they actually typed instead.
    """
    if text.strip():
        return detect_language(text)

    for msg in reversed(history):
        if msg.sender_type == "customer" and msg.content:
            return detect_language(msg.content)

    return "english"


# ── Escalation ────────────────────────────────────────────────────────────────

#: The AI appends this when the seller needs to step in. A fixed marker rather
#: than keyword-matching the reply: the reply may be Khmer, romanized Khmer or
#: English, and "I'll check with the seller" has no stable spelling across all
#: three — matching prose would be both fragile and language-biased.
NEEDS_SELLER_MARKER = "[[NEEDS_SELLER]]"


def split_needs_seller(reply: str) -> tuple[str, bool]:
    """Strip the escalation marker from a reply; report whether it was there.

    Stripping is unconditional, not conditional on the flag: if the model ever
    emits the marker somewhere unexpected, the customer must still never see it.
    """
    if NEEDS_SELLER_MARKER not in reply:
        return reply, False
    return reply.replace(NEEDS_SELLER_MARKER, "").strip(), True


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(user: User, products: list[Product], detected_lang: str) -> str:
    business = user.business_name or user.full_name

    product_lines: list[str] = []
    for p in products:
        if not p.is_active:
            continue
        line = f"• {p.name} — ${p.price:,.2f}"
        if p.description:
            line += f"\n  {p.description}"
        if p.stock == 0:
            line += "\n  ⚠️ OUT OF STOCK"
        else:
            line += f"\n  Stock: {p.stock} units"
        product_lines.append(line)

    product_catalog = (
        "\n\n".join(product_lines)
        if product_lines
        else "No products are listed yet."
    )

    lang_instruction = {
        "khmer": "The customer is writing in Khmer (ខ្មែរ). Reply ONLY in Khmer script.",
        "romanized_khmer": (
            "The customer is writing in romanized/phonetic Khmer (Latin script). "
            "Reply in the same romanized Khmer style — short, casual, like a real shop owner on Messenger. "
            "You may mix in a little English for product names or prices."
        ),
        "english": "The customer is writing in English. Reply in English.",
    }.get(detected_lang, "Match the customer's language exactly.")

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
{lang_instruction}

ALWAYS stay consistent with the customer's language throughout the conversation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
BEHAVIOR RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Give prices directly — never make a customer ask twice
- If an item is out of stock, apologize and suggest alternatives if available
- Keep replies short (2–4 sentences max unless more detail is truly needed)
- Never invent products that are not in the catalog above
- If you cannot answer, politely say you will check with the seller, and append
  the marker {NEEDS_SELLER_MARKER} as the very last thing in your reply
- Also append that marker if the customer asks for a discount, complains, wants
  a refund, or asks anything only the seller can decide
- The marker is for the seller's dashboard, never for the customer: never
  explain it, translate it, or mention that it exists

━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHOTOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Customers often send a photo instead of describing what they want
- Match the photo to a product in the catalog above and answer about that one
- If the photo matches nothing in the catalog, say so plainly and offer to
  check with the seller — do NOT guess at a product or invent a price
- A photo may arrive with no words at all: treat it as "do you have this, and
  how much?" and answer in the language the customer has been using
"""


# ── Conversation history builder ──────────────────────────────────────────────

_HISTORY_LIMIT = 20  # last N messages sent to OpenAI (controls token usage)

# Images are billed by the tile and dwarf text tokens, so only the most recent
# few are re-sent. Older ones degrade to a text placeholder: the model still
# knows a photo was sent and roughly when, without paying for it every turn.
_HISTORY_IMAGE_LIMIT = 2

_IMAGE_PLACEHOLDER = "[the customer sent a photo]"


def image_urls(message: Message) -> list[str]:
    """Hosted URLs of a message's image attachments."""
    return [
        a.file_url
        for a in (message.attachments or [])
        if (a.file_type or "").startswith("image")
    ]


def _user_content(text: str, image_urls_: list[str]) -> str | list[dict]:
    """Build one user turn, multimodal only when there's actually an image.

    A plain string is returned for text-only turns rather than a single-element
    list — it's the overwhelmingly common case and keeps the payload small.
    """
    if not image_urls_:
        return text

    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in image_urls_
    )
    return parts


def build_openai_messages(
    system_prompt: str,
    history: list[Message],
    new_message: str,
    new_image_urls: list[str] | None = None,
) -> list[dict]:
    """Assemble the chat payload, carrying images the customer has sent.

    History images matter: a customer typically sends a photo and *then* asks
    "how much is this?" as a separate message, so dropping the photo from
    history would leave the model answering a question about nothing.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    recent = history[-_HISTORY_LIMIT:]

    # Decide which historical images survive before emitting, so the newest
    # ones win rather than whichever appears first.
    keep_images: set[str] = set()
    budget = _HISTORY_IMAGE_LIMIT
    for msg in reversed(recent):
        if budget <= 0:
            break
        for url in image_urls(msg):
            if budget <= 0:
                break
            keep_images.add(url)
            budget -= 1

    for msg in recent:
        urls = image_urls(msg)
        kept = [u for u in urls if u in keep_images]
        text = msg.content or ""

        # An image-only message has no text; without a stand-in it would be
        # skipped entirely and the thread would read as if it never happened.
        if urls and not kept and not text:
            text = _IMAGE_PLACEHOLDER
        if not text and not kept:
            continue

        if msg.sender_type == "customer":
            messages.append({"role": "user", "content": _user_content(text, kept)})
        elif msg.sender_type in ("assistant", "seller"):
            # Assistant turns are text-only: the model never sends images.
            messages.append({"role": "assistant", "content": text or _IMAGE_PLACEHOLDER})

    messages.append(
        {"role": "user", "content": _user_content(new_message, new_image_urls or [])}
    )
    return messages


# ── OpenAI call ───────────────────────────────────────────────────────────────

async def generate_ai_reply(openai_messages: list[dict]) -> str:
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=openai_messages,
        temperature=0.65,
        max_tokens=400,
    )
    return response.choices[0].message.content or ""
