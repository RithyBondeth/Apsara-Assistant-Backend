"""The translatable-error contract.

Error codes are an API contract the web app depends on: it maps each one to a
Khmer/English string. A code that changes name, or a new error that never gets
registered, shows the seller an untranslated fallback — which is precisely the
gap this system exists to close. These tests make that fail loudly instead.
"""
from __future__ import annotations

import inspect

from app.core import errors


def _factories():
    """Every public error factory in the module."""
    return [
        fn
        for name, fn in inspect.getmembers(errors, inspect.isfunction)
        if not name.startswith("_") and fn.__module__ == errors.__name__
    ]


def test_every_factory_code_is_registered():
    """ALL_ERROR_CODES is what the frontend translates against.

    A factory whose code isn't listed would reach the UI with no Khmer string.
    """
    for factory in _factories():
        # Fill any required params with a placeholder — we only want the code.
        params = inspect.signature(factory).parameters
        args = ["x"] * len(params)
        error = factory(*args)
        assert error.code in errors.ALL_ERROR_CODES, (
            f"{factory.__name__}() emits {error.code!r}, which is missing from "
            "ALL_ERROR_CODES — add it there and to language/en.json + km.json"
        )


def test_registry_has_no_codes_without_a_factory():
    """Guards the other direction: a stale entry left behind after a rename."""
    emitted = set()
    for factory in _factories():
        params = inspect.signature(factory).parameters
        emitted.add(factory(*(["x"] * len(params))).code)

    # validation_error is raised by main.py's handler, not a factory here.
    orphans = errors.ALL_ERROR_CODES - emitted - {"validation_error"}
    assert not orphans, f"registered but never raised: {sorted(orphans)}"


def test_error_body_carries_code_message_and_params():
    """The shape the frontend parses."""
    error = errors.insufficient_stock("Khmer Silk Scarf", 3)

    assert error.status_code == 400
    assert error.detail == {
        "code": "insufficient_stock",
        "message": "Insufficient stock for 'Khmer Silk Scarf' (available: 3)",
        # Params are what the Khmer string interpolates — without them the
        # translation could only ever be generic.
        "params": {"name": "Khmer Silk Scarf", "available": 3},
    }


def test_english_message_is_always_present():
    """The fallback for a code the deployed frontend doesn't know yet.

    Backend deploys ahead of the web app routinely; without this the seller
    would get an empty error box instead of an English sentence.
    """
    for factory in _factories():
        params = inspect.signature(factory).parameters
        error = factory(*(["x"] * len(params)))
        message = error.detail["message"]
        assert message and message.strip(), f"{factory.__name__}() has no message"


def test_validation_errors_use_the_same_shape(client):
    """FastAPI's own 422 is remapped — it's the last English-only path."""
    r = client.post("/api/v1/auth/register", json={"email": "not-an-email"})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "validation_error"
    assert "field" in detail["params"]


def test_seller_facing_errors_are_translatable_but_webhooks_are_not(client):
    """Platform-facing webhook errors deliberately stay plain strings.

    Telegram and Meta read those; no seller ever does. Translating them would
    add codes nobody renders.
    """
    r = client.post("/api/v1/webhooks/telegram/00000000-0000-0000-0000-000000000000", json={})

    assert r.status_code == 404
    assert isinstance(r.json()["detail"], str)
