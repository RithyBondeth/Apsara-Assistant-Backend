"""Language detection — the product's core promise.

Answering a customer in a language they didn't write in is the most visible
way this product can fail, so the English/Khmer boundary is pinned down here.
"""
from __future__ import annotations

import pytest

from app.services.ai_service import detect_language


@pytest.mark.parametrize(
    "text",
    [
        # Every one of these was previously misread as romanized Khmer, because
        # the hints were matched as substrings: "ho" inside "how", "ot" inside
        # "not"/"lot"/"photo", "som" inside "some", "ning" inside "morning",
        # "oun" inside "amount"/"account".
        "How much is this?",
        "Do you have some in red?",
        "Good morning!",
        "Can you send a photo?",
        "What is the total amount?",
        "I have not got it yet",
        "My account number",
        "Is it hot outside?",
        "Thanks a lot",
        # Ordinary English words that are also romanized Khmer hints. One alone
        # is not evidence.
        "What do you mean?",
        "Minimum order?",
        "Hello",
        "Do you ship to Phnom Penh?",
    ],
)
def test_english_is_not_mistaken_for_khmer(text):
    assert detect_language(text) == "english"


@pytest.mark.parametrize(
    "text",
    [
        "thlai ponman?",
        "som chuon",
        "bong mean krama te?",
        "ot tov",
        "orkoun charan",
        "khnhom chng tinh",
        "nih thlai ponman",
    ],
)
def test_romanized_khmer_is_still_detected(text):
    assert detect_language(text) == "romanized_khmer"


@pytest.mark.parametrize("text", ["តើអ្នកមានក្រមាទេ?", "សួស្តី", "ខ្ញុំចង់ទិញ"])
def test_khmer_script_wins_immediately(text):
    assert detect_language(text) == "khmer"


def test_two_ambiguous_words_together_do_count():
    """Each is an English word alone, but together they're a Khmer phrase."""
    assert detect_language("bong mean") == "romanized_khmer"


def test_khmer_script_beats_english_words():
    assert detect_language("how much? តើថ្លៃប៉ុន្មាន") == "khmer"
