"""One source of "now" for the whole application.

Every timestamp in the schema is a naive `DateTime` — Postgres `TIMESTAMP
WITHOUT TIME ZONE` — holding UTC by convention. Comparing one of those against
a timezone-aware datetime raises `TypeError`, so the app cannot simply move to
`datetime.now(timezone.utc)`; doing so would break token expiry, the job
queue's `run_after`, and every code lifetime at once.

This returns exactly what `datetime.utcnow()` returned — naive UTC — without
the deprecation. `utcnow` is deprecated from Python 3.12 and slated for removal,
and it is a footgun besides: it stamps a UTC reading with no tzinfo, so the
result silently compares as if it were local time.

Routing every timestamp through one function also gives tests a single place to
freeze, and leaves one edit to make if the columns ever move to `timestamptz`.
"""

from datetime import date, datetime, timezone


def utcnow() -> datetime:
    """The current UTC time, naive, matching the schema's columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utctoday() -> date:
    """Today's date in UTC.

    `date.today()` reads the server's local timezone, which would put the daily
    reply quota on a different clock from every other timestamp here: on a
    machine set to Asia/Phnom_Penh the quota resets at 17:00 UTC, seven hours
    out from the day boundary the rest of the app uses. Agreeing only on a
    UTC-configured server is the kind of difference that shows up as a quota
    resetting at the wrong hour long after deploy.
    """
    return datetime.now(timezone.utc).date()
