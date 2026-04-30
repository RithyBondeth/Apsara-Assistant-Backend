from __future__ import annotations

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


def detect_language(text: str) -> str:
    """
    Detect whether the message is:
      - 'khmer'           — contains Khmer Unicode characters
      - 'romanized_khmer' — phonetic Khmer written in Latin script
      - 'english'         — standard English
    """
    if any(ord(c) in _KHMER_UNICODE_RANGE for c in text):
        return "khmer"

    lower = text.lower()
    if any(hint in lower for hint in _ROMANIZED_KHMER_HINTS):
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
- If you cannot answer, politely say you will check with the seller
"""


# ── Conversation history builder ──────────────────────────────────────────────

_HISTORY_LIMIT = 20  # last N messages sent to OpenAI (controls token usage)


def build_openai_messages(
    system_prompt: str,
    history: list[Message],
    new_message: str,
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    recent = history[-_HISTORY_LIMIT:]
    for msg in recent:
        if not msg.content:
            continue
        if msg.sender_type == "customer":
            messages.append({"role": "user", "content": msg.content})
        elif msg.sender_type in ("assistant", "seller"):
            messages.append({"role": "assistant", "content": msg.content})

    messages.append({"role": "user", "content": new_message})
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
