"""Helpers for driving the assistant without reaching the Claude API."""

import types
from contextlib import contextmanager
from unittest import mock

from app.services import ai_service


class Captured(dict):
    """The keyword arguments of the most recent messages call."""

    @property
    def system_prompt(self) -> str:
        return self["system"]

    @property
    def turns(self) -> list[dict]:
        return self["messages"]


def response(text):
    """A Messages API response carrying `text` — or no text at all for None."""
    content = [types.SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(types.SimpleNamespace(type="text", text=text))
    return types.SimpleNamespace(content=content, stop_reason="end_turn", stop_details=None)


@contextmanager
def replies(text="Bat, mean nov ban."):
    """Stub the model, yielding a record of what it was asked."""
    captured = Captured()

    def create(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return response(text)

    client = mock.MagicMock()
    client.beta.messages.create.side_effect = create
    with mock.patch.object(ai_service, "_client", return_value=client):
        yield captured


@contextmanager
def fails(exc):
    """Stub the model so every call raises `exc`."""
    client = mock.MagicMock()
    client.beta.messages.create.side_effect = exc
    with mock.patch.object(ai_service, "_client", return_value=client):
        yield
