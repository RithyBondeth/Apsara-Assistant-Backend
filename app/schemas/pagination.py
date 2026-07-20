"""Shared pagination envelope for list endpoints.

Every list that can truncate returns a ``Page`` rather than a bare array. The
distinction matters: an array of 50 is indistinguishable from "the first 50 of
300", which is how a seller ended up unable to see — or reach — their own older
products. ``total`` is what makes a list navigable.

Lists that cannot truncate (integrations, where a seller has a handful of
channels and the query is unbounded) deliberately keep returning a bare array;
there is no page to report, and wrapping them would be shape churn for its own
sake.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy.orm import Query as SAQuery

T = TypeVar("T")

# Upper bound on one page. Clients needing everything (order-form pickers) page
# through, so no caller has to guess a "big enough" number — and no caller can
# ask the server to serialise an entire table in one request.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int


def SkipParam() -> int:  # noqa: N802 - reads as a FastAPI dependency at call sites
    return Query(default=0, ge=0)


def LimitParam() -> int:  # noqa: N802
    return Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


def paginate(query: SAQuery, skip: int, limit: int) -> tuple[list, int]:
    """Return ``(page_items, total)`` for an already-filtered, already-ordered query.

    The count runs on the same filtered query the items come from, so `total`
    can never describe a different set than the page — a pager built on a
    mismatched count advertises pages that render empty.
    """
    total = query.count()
    items = query.offset(skip).limit(limit).all()
    return items, total
