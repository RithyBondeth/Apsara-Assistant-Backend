"""Helpers for driving the assistant without reaching OpenAI."""

import types
from contextlib import contextmanager
from unittest import mock

from app.services import ai_service


class Captured(dict):
    """The keyword arguments of the most recent completions call."""

    @property
    def system_prompt(self) -> str:
        return self["messages"][0]["content"]

    @property
    def turns(self) -> list[dict]:
        return self["messages"][1:]


@contextmanager
def replies(text="Bat, mean nov ban."):
    """Stub the model, yielding a record of what it was asked."""
    captured = Captured()

    def create(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))]
        )

    client = mock.MagicMock()
    client.chat.completions.create.side_effect = create
    with mock.patch.object(ai_service, "_client", return_value=client):
        yield captured


@contextmanager
def fails(exc):
    """Stub the model so every call raises `exc`."""
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = exc
    with mock.patch.object(ai_service, "_client", return_value=client):
        yield
