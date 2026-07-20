"""Free-text search over list endpoints.

One helper rather than an ILIKE spelled out per endpoint, because the escaping
is the part that's easy to get wrong: a customer searching for "50%" or an
email containing "_" would otherwise be handing LIKE its own wildcards and
getting silently wrong results.

``ilike`` is used rather than a raw ILIKE so the tests keep working — SQLite
has no ILIKE, and SQLAlchemy compiles this to ``lower(x) LIKE lower(y)`` there
while still emitting real ILIKE on Postgres.
"""
from __future__ import annotations

from sqlalchemy import or_

# Backslash is first: escaping it after the others would double-escape the
# escapes this function just inserted.
_LIKE_SPECIALS = ("\\", "%", "_")


def like_pattern(term: str) -> str:
    """Turn user input into a safe "contains" pattern."""
    escaped = term.strip()
    for char in _LIKE_SPECIALS:
        escaped = escaped.replace(char, f"\\{char}")
    return f"%{escaped}%"


def search_clause(term: str, *columns):
    """Match ``term`` against any of ``columns``, case-insensitively.

    Returns None for a blank term so callers can skip the filter entirely
    rather than applying a match-everything ``%%`` pattern.
    """
    if not term or not term.strip():
        return None

    pattern = like_pattern(term)
    return or_(*[column.ilike(pattern, escape="\\") for column in columns])
