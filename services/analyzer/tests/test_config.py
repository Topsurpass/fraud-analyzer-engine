from app.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.default_row_limit == 1000
    assert s.max_row_limit == 10000
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
