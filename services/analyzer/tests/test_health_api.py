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
