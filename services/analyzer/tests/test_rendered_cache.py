"""The rendered-response cache: bytes built once, served to everyone.

Asserted as ratios and byte equality, never as timings. The claim is that N
viewers of one result cost what one viewer costs, and that either holds or it
does not, independent of how loaded the machine is.
"""

from __future__ import annotations

import gzip

import orjson
import pytest

from app.services import rendered_cache


@pytest.fixture(autouse=True)
def _clean():
    rendered_cache.clear()
    yield
    rendered_cache.clear()


def _body(rows=3):
    return {
        "query_id": "q1",
        "data_hash": "sha256:abc",
        "poll_interval_ms": 5000,
        "columns": ["a", "b"],
        "rows": [[i, f"row-{i}" * 40] for i in range(rows)],
        "changed": True,
        "from_cache": True,
    }


def test_the_second_viewer_does_not_pay_to_render_again():
    """The whole point. Ten analysts on one board is one render, not ten."""
    body = _body(200)
    key = rendered_cache.key_for("q1", "sha256:abc", 5000, True)
    builds = 0

    def build():
        nonlocal builds
        builds += 1
        return body

    for _ in range(10):
        rendered_cache.get_or_render(key, build)

    assert builds == 1
    stats = rendered_cache.stats()
    assert stats["hits"] == 9 and stats["misses"] == 1


def test_the_bytes_served_are_the_body_that_went_in():
    """A faster path that returns different data is not a faster path."""
    body = _body(200)
    key = rendered_cache.key_for("q1", "sha256:abc", 5000, True)
    rendered = rendered_cache.get_or_render(key, lambda: body)

    assert rendered.compressed
    assert orjson.loads(gzip.decompress(rendered.body)) == body


def test_a_different_result_is_a_different_entry():
    """data_hash is the identity. Two results must never share bytes."""
    first = rendered_cache.get_or_render(
        rendered_cache.key_for("q1", "sha256:aaa", 5000, True), lambda: _body(5)
    )
    second = rendered_cache.get_or_render(
        rendered_cache.key_for("q1", "sha256:bbb", 5000, True), lambda: _body(9)
    )
    assert first.body != second.body


def test_two_queries_with_identical_data_do_not_share_an_entry():
    """Otherwise evicting one would silently blank the other."""
    rendered_cache.get_or_render(
        rendered_cache.key_for("q1", "sha256:same", 5000, True), lambda: _body(5)
    )
    rendered_cache.get_or_render(
        rendered_cache.key_for("q2", "sha256:same", 5000, True), lambda: _body(5)
    )
    assert rendered_cache.stats()["entries"] == 2


def test_small_payloads_are_stored_uncompressed():
    """Below the threshold the handshake costs more than the bytes save."""
    tiny = {"query_id": "q", "data_hash": "h", "changed": False}
    rendered = rendered_cache.get_or_render(
        rendered_cache.key_for("q", "h", 5000, False), lambda: tiny
    )
    assert not rendered.compressed
    assert orjson.loads(rendered.body) == tiny


def test_invalidating_a_query_drops_every_rendering_of_it():
    """An edited query must not keep serving bytes built from the old one."""
    for hashed in ("sha256:a", "sha256:b"):
        rendered_cache.get_or_render(
            rendered_cache.key_for("q1", hashed, 5000, True), lambda: _body(5)
        )
    rendered_cache.get_or_render(
        rendered_cache.key_for("q2", "sha256:c", 5000, True), lambda: _body(5)
    )

    rendered_cache.invalidate_query("q1")

    assert rendered_cache.stats()["entries"] == 1
    assert rendered_cache.get(rendered_cache.key_for("q1", "sha256:a", 5000, True)) is None


def test_result_cache_invalidation_reaches_the_rendered_bytes():
    """The two are coupled inside result_cache.invalidate on purpose: a caller
    that dropped one and kept the other would serve a stale chart that no
    amount of refreshing could fix."""
    from app.services import result_cache

    rendered_cache.get_or_render(
        rendered_cache.key_for("q9", "sha256:x", 5000, True), lambda: _body(5)
    )
    result_cache.invalidate("q9")

    assert rendered_cache.get(rendered_cache.key_for("q9", "sha256:x", 5000, True)) is None


def test_the_cache_stays_inside_its_byte_budget(monkeypatch):
    """Unbounded, this is an OOM in a container rather than a cache."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rendered_cache_max_bytes", 20_000, raising=False)

    for i in range(60):
        rendered_cache.get_or_render(
            rendered_cache.key_for(f"q{i}", f"sha256:{i}", 5000, True),
            lambda i=i: _body(300),
        )

    assert rendered_cache.total_bytes() <= 20_000
    assert rendered_cache.stats()["entries"] < 60
