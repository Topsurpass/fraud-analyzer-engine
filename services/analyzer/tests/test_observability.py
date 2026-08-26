"""Logging and request correlation.

Nothing configured the root logger, so uvicorn's default left root at WARNING
and every logger.info in the service was discarded -- including the startup
banner in app.db.migrate that the README tells operators to read to confirm
which backend a deployment is actually using. That line was documented and
unreachable.
"""

from __future__ import annotations

import logging

import pytest

from app.config import get_settings
from app.observability import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    configure_logging,
    request_id_var,
)


def test_configure_logging_installs_a_root_handler(monkeypatch):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_fae_configured", False):
            root.removeHandler(handler)

    monkeypatch.setenv("FAE_LOG_LEVEL", "INFO")
    get_settings.cache_clear()
    configure_logging()

    assert any(getattr(h, "_fae_configured", False) for h in root.handlers)
    assert root.level == logging.INFO


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setenv("FAE_LOG_LEVEL", "INFO")
    get_settings.cache_clear()
    configure_logging()
    before = len(logging.getLogger().handlers)
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == before


def test_startup_banner_is_actually_emitted(app_db, caplog, monkeypatch):
    """The regression: this line was documented and silently discarded."""
    monkeypatch.setenv("FAE_LOG_LEVEL", "INFO")
    get_settings.cache_clear()

    from app.db.migrate import bootstrap_schema

    with caplog.at_level(logging.INFO, logger="app.db.migrate"):
        bootstrap_schema()

    assert any("App-state backend=" in record.message for record in caplog.records)


def test_banner_reports_which_setting_actually_won(app_db, caplog, monkeypatch):
    """FAE_APP_DB_URL overrides FAE_DB_BACKEND, and the banner must say so.

    A banner reading "backend=neon" beside a sqlite:// URL is the exact
    confusion this line exists to prevent.
    """
    monkeypatch.setenv("FAE_DB_BACKEND", "neon")
    monkeypatch.setenv("FAE_LOG_LEVEL", "INFO")
    get_settings.cache_clear()

    from app.db.migrate import bootstrap_schema

    with caplog.at_level(logging.INFO, logger="app.db.migrate"):
        bootstrap_schema()

    banner = next(r.message for r in caplog.records if "App-state backend=" in r.message)
    assert "backend=sqlite" in banner
    assert "FAE_APP_DB_URL override" in banner


def test_ephemeral_storage_warning_names_the_file_in_use(caplog, monkeypatch):
    """It used to print the sqlite_app_db_path setting, which is not in use
    when FAE_APP_DB_URL overrides it -- sending the operator to the wrong file."""
    from app.db.migrate import warn_if_storage_is_ephemeral

    with caplog.at_level(logging.WARNING, logger="app.db.migrate"):
        warn_if_storage_is_ephemeral()

    warning = next(r.message for r in caplog.records if "App-state is SQLite" in r.message)
    assert "./fraud_analyzer.db" not in warning
    assert str(get_settings().app_db_sqlite_file) in warning


def test_request_id_is_echoed_back(admin_client):
    response = admin_client.get("/health")
    assert response.headers[REQUEST_ID_HEADER]


def test_inbound_request_id_is_honoured(admin_client):
    response = admin_client.get("/health", headers={REQUEST_ID_HEADER: "trace-me-42"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-me-42"


def test_each_request_gets_a_distinct_id(admin_client):
    first = admin_client.get("/health").headers[REQUEST_ID_HEADER]
    second = admin_client.get("/health").headers[REQUEST_ID_HEADER]
    assert first != second


def test_filter_attaches_the_current_request_id():
    token = request_id_var.set("abc123")
    try:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "m", None, None)
        RequestIdFilter().filter(record)
        assert record.request_id == "abc123"
    finally:
        request_id_var.reset(token)


def test_json_formatter_emits_one_object_per_line():
    import json

    from app.observability import JsonFormatter

    record = logging.LogRecord("t", logging.WARNING, "f", 1, "hello %s", ("world",), None)
    record.request_id = "rid"
    parsed = json.loads(JsonFormatter().format(record))

    assert parsed["message"] == "hello world"
    assert parsed["level"] == "WARNING"
    assert parsed["request_id"] == "rid"
