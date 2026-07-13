"""Tests for environment-aware settings."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _make(**overrides) -> Settings:
    base = {"DATABASE_URL": "sqlite://", "SECRET_KEY": "x"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_cors_origins_parsed_to_list():
    s = _make(CORS_ORIGINS="http://a.com, https://b.com ,")
    assert s.cors_origins_list == ["http://a.com", "https://b.com"]


def test_is_production_flag():
    assert _make(ENVIRONMENT="production").is_production is True
    assert _make(ENVIRONMENT="local").is_production is False


def test_invalid_environment_rejected():
    with pytest.raises(ValidationError):
        _make(ENVIRONMENT="staging")


@pytest.mark.parametrize("env", ["local", "dev", "production"])
def test_valid_environments_accepted(env):
    assert _make(ENVIRONMENT=env).ENVIRONMENT == env
