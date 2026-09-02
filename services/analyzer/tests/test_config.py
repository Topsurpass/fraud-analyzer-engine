from app.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.default_row_limit == 1000
    # Clears the 25,000-row monitoring workload rather than sitting on it.
    assert s.max_row_limit == 50000
    assert s.preview_row_limit == 100
    assert s.query_timeout_s == 10
    assert s.poll_interval_ms == 5000


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("FAE_DEFAULT_ROW_LIMIT", "50")
    assert Settings(_env_file=None).default_row_limit == 50


def test_cors_origin_list_splits():
    s = Settings(_env_file=None, cors_origins="http://a.com, http://b.com")
    assert s.cors_origin_list == ["http://a.com", "http://b.com"]


def test_query_timeout_ms():
    assert Settings(_env_file=None, query_timeout_s=7).query_timeout_ms == 7000


def test_max_below_default_rejected():
    import pytest
    with pytest.raises(ValueError):
        Settings(_env_file=None, default_row_limit=500, max_row_limit=100)


def test_socket_timeout_outlasts_the_server_statement_timeout():
    """Regression: the two must not fire together.

    When pymysql's socket read_timeout equalled the server's
    max_execution_time, the socket usually won the race and a merely slow
    query surfaced as errno 2013 "lost connection" -> 502 DB_UNREACHABLE,
    instead of errno 3024 -> 504 QUERY_TIMEOUT. A 502 tells the frontend the
    database is down, which is the wrong signal and the wrong user action.
    """
    s = Settings(_env_file=None, query_timeout_s=10)
    assert s.socket_read_timeout_s > s.query_timeout_s


def test_socket_grace_is_configurable(monkeypatch):
    monkeypatch.setenv("FAE_SOCKET_TIMEOUT_GRACE_S", "30")
    s = Settings(_env_file=None, query_timeout_s=10)
    assert s.socket_read_timeout_s == 40
