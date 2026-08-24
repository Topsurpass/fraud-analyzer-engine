"""Liveness and readiness are different questions.

/health used to be the only probe and returned ok unconditionally without ever
touching a database, so an instance whose app-state backend was unreachable
reported healthy to the orchestrator while every real request returned a 500.
"""

from __future__ import annotations

import pytest

import app.main as main


def test_health_is_liveness_and_does_not_touch_the_database(client, monkeypatch):
    """A dependency outage must not make the orchestrator kill a working process."""
    def explode():
        raise RuntimeError("database is down")

    monkeypatch.setattr(main, "get_engine", explode)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ok_when_the_app_state_database_answers(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_fails_when_the_app_state_database_is_unreachable(client, monkeypatch):
    def explode():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main, "get_engine", explode)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error_code"] == "SERVICE_NOT_READY"


# ---------------------------------------------------------------------------
# Startup credential check
#
# A Fernet key that changed between the save and the read makes every query on
# a connection fail, one request at a time, on a connection whose every visible
# field is correct. It is the same confusion each time, so it belongs in the
# log at boot.
# ---------------------------------------------------------------------------


def test_startup_names_the_connections_whose_password_cannot_be_read(
    client, target_sqlite, caplog
):
    import logging

    from app.main import _report_unreadable_credentials

    client.post(
        "/connections",
        json={
            "name": "pg-unreadable",
            "db_type": "postgres",
            "host": "127.0.0.1",
            "port": 1,
            "database": "d",
            "username": "u",
            "password": "stored-under-another-key",
        },
    )

    def boom(_token):
        from cryptography.fernet import InvalidToken

        raise InvalidToken()

    with caplog.at_level(logging.WARNING):
        import app.main as main_module

        original = main_module.__dict__.get("decrypt")
        try:
            import app.security.crypto as crypto

            saved = crypto.decrypt
            crypto.decrypt = boom
            _report_unreadable_credentials()
        finally:
            crypto.decrypt = saved
            if original is not None:  # pragma: no cover
                main_module.decrypt = original

    assert "pg-unreadable" in caplog.text
    assert "FAE_FERNET_KEY" in caplog.text


def test_startup_logs_the_key_fingerprint_but_never_the_key(client, caplog):
    import logging

    from app.config import get_settings
    from app.main import _report_unreadable_credentials

    with caplog.at_level(logging.INFO):
        _report_unreadable_credentials()

    key = get_settings().fernet_key or ""
    assert "fingerprint" in caplog.text.lower()
    # The whole point of a fingerprint is that it is not the secret.
    assert key not in caplog.text


def test_startup_never_blocks_on_a_broken_check(monkeypatch, caplog):
    """A diagnostic must never be the reason the service will not start."""
    import logging

    import app.main as main_module

    def boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(main_module, "get_settings", boom)
    with caplog.at_level(logging.ERROR):
        main_module._report_unreadable_credentials()  # must not raise
    assert "continuing" in caplog.text
