"""Logging, health checks, request tagging and the CORS guard."""

import json
import logging

import pytest

from app.core import logging as applog
from app.core import observability
from app.core.config import Settings


# ── Logging ──────────────────────────────────────────────────────────────────

def test_json_lines_are_parseable():
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1,
                               "hello %s", ("world",), None)
    payload = json.loads(applog.JsonFormatter().format(record))

    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["timestamp"].endswith("+00:00"), "timestamps must carry a zone"


def test_extras_reach_the_log_line():
    """The request middleware attaches ids this way; they must survive."""
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "req", (), None)
    record.request_id = "abc123"
    record.duration_ms = 12.5

    payload = json.loads(applog.JsonFormatter().format(record))

    assert payload["request_id"] == "abc123"
    assert payload["duration_ms"] == 12.5


def test_exceptions_are_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord("app.test", logging.ERROR, __file__, 1,
                                   "failed", (), sys.exc_info())

    payload = json.loads(applog.JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


def test_configuring_twice_does_not_duplicate_output():
    """The API and the worker both call this; a second call must replace
    handlers rather than add to them, or every line appears twice."""
    applog.configure("INFO", "json")
    applog.configure("DEBUG", "text")

    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger().level == logging.DEBUG
    applog.configure("INFO", "text")


# ── Error tracking ───────────────────────────────────────────────────────────

def test_error_tracking_is_off_without_a_dsn(monkeypatch):
    monkeypatch.setattr(observability.settings, "SENTRY_DSN", "")
    assert observability.configure() is False


def test_a_dsn_without_the_library_degrades_rather_than_crashing(monkeypatch):
    """The dependency is optional, so a misconfiguration must not stop the app
    from starting."""
    monkeypatch.setattr(observability.settings, "SENTRY_DSN", "https://x@example/1")
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", None)

    import builtins
    real_import = builtins.__import__

    def fail_sentry(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sentry)
    assert observability.configure() is False


# ── CORS ─────────────────────────────────────────────────────────────────────

def base_settings(**kw):
    return Settings(DATABASE_URL="postgresql://x/y", SECRET_KEY="k", **kw)


def test_development_may_use_a_wildcard():
    assert base_settings(ENVIRONMENT="development").cors_origins == ["*"]


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_anything_else_refuses_a_wildcard(environment):
    """Credentialed requests from any origin is not a thing to deploy."""
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        base_settings(ENVIRONMENT=environment).cors_origins


def test_a_configured_list_is_split_and_trimmed():
    settings = base_settings(ENVIRONMENT="production",
                             CORS_ORIGINS="https://a.example.com, https://b.example.com")
    assert settings.cors_origins == ["https://a.example.com", "https://b.example.com"]


# ── Health ───────────────────────────────────────────────────────────────────

def test_liveness_does_not_depend_on_the_database(client):
    """Kept trivial so a database blip cannot take every instance out of
    rotation at once."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_reports_the_database(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_readiness_fails_when_the_database_is_unreachable(client, monkeypatch):
    import app.main as main

    class Broken:
        def execute(self, *_):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", Broken)
    r = client.get("/health/ready")

    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"


# ── Request tagging ──────────────────────────────────────────────────────────

def test_every_response_carries_a_request_id(client):
    assert client.get("/health").headers["X-Request-ID"]


def test_an_inbound_request_id_is_kept(client):
    """So a trace started by a proxy or the web app carries through instead of
    restarting at this hop."""
    r = client.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["X-Request-ID"] == "trace-me-123"
