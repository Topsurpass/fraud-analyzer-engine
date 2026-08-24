"""Choosing target addresses this machine can reach.

The bug this exists for: Neon publishes both A and AAAA records, the container
has no IPv6 route, and Docker's embedded resolver under WSL2 intermittently
returns *only* the AAAA records. Every attempt then fails with "Network is
unreachable" against addresses that were never reachable, while the server sits
there answering on IPv4.

The seams (``_resolve`` and ``has_global_ipv6``) are patched rather than
mocked at the socket layer, so these tests state the policy rather than
re-testing glibc.
"""

from __future__ import annotations

import socket

import pytest

from app.db import addressing

pytestmark = pytest.mark.real_addressing

V4 = ("23.21.74.185", "98.89.62.209")
V6 = ("2600:1f18:6fa0:b312:d68e:a301:d146:2c7",)


def _clear(function) -> None:
    # Several tests replace these with plain callables, and teardown can run
    # while the replacement is still installed.
    clear = getattr(function, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _clear_caches():
    _clear(addressing.routable_addresses)
    _clear(addressing.has_global_ipv6)
    yield
    _clear(addressing.routable_addresses)
    _clear(addressing.has_global_ipv6)


@pytest.fixture
def resolver(monkeypatch):
    """Installs a fake DNS whose answer can differ per address family."""

    calls: list[int] = []

    def install(*, unspec, inet=None):
        def _resolve(host, port, family):
            calls.append(family)
            if family == socket.AF_INET:
                return list(inet if inet is not None else [])
            return list(unspec)

        monkeypatch.setattr(addressing, "_resolve", _resolve)
        return calls

    return install


def test_a_dual_stack_machine_is_left_alone(monkeypatch, resolver):
    # Nothing here is a policy about address families. It is a fix for one
    # container condition, and a host with working IPv6 does not have it.
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: True)
    calls = resolver(unspec=V6)
    assert addressing.routable_addresses("db.example.test", 5432) == ()
    assert calls == []  # not even resolved: there is nothing to decide


def test_ipv6_is_dropped_when_it_cannot_be_routed(monkeypatch, resolver):
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    resolver(unspec=[*V4, *V6])
    assert addressing.routable_addresses("db.example.test", 5432) == V4


def test_an_ipv6_only_answer_triggers_a_second_lookup(monkeypatch, resolver):
    """The actual bug. AF_UNSPEC volunteers only AAAA; the A records exist."""
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    calls = resolver(unspec=V6, inet=V4)

    assert addressing.routable_addresses("db.example.test", 5432) == V4
    assert calls == [socket.AF_UNSPEC, socket.AF_INET]


def test_no_opinion_when_the_name_does_not_resolve_at_all(monkeypatch, resolver):
    # A genuine NXDOMAIN must fall through to libpq, which reports it better
    # than anything invented here would.
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    resolver(unspec=[], inet=[])
    assert addressing.routable_addresses("nope.example.test", 5432) == ()


def test_an_address_literal_is_never_second_guessed(monkeypatch, resolver):
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    calls = resolver(unspec=V4)
    assert addressing.routable_addresses("10.0.0.5", 5432) == ()
    assert calls == []


def test_order_is_preserved(monkeypatch, resolver):
    # libpq tries the addresses in the order given, and the resolver's order
    # is the one it sorted for this host.
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    resolver(unspec=["1.1.1.1", "2.2.2.2", "3.3.3.3"])
    assert addressing.routable_addresses("db.example.test", 5432) == (
        "1.1.1.1",
        "2.2.2.2",
        "3.3.3.3",
    )


def test_the_answer_is_cached(monkeypatch, resolver):
    monkeypatch.setattr(addressing, "has_global_ipv6", lambda: False)
    calls = resolver(unspec=V4)
    addressing.routable_addresses("db.example.test", 5432)
    addressing.routable_addresses("db.example.test", 5432)
    assert len(calls) == 1


class TestHasGlobalIpv6:
    def _procfs(self, monkeypatch, tmp_path, contents):
        path = tmp_path / "if_inet6"
        path.write_text(contents)
        monkeypatch.setattr(addressing, "_IF_INET6", path)

    def test_loopback_alone_is_not_a_route(self, monkeypatch, tmp_path):
        # Exactly what a default Docker container looks like.
        self._procfs(
            monkeypatch, tmp_path, "00000000000000000000000000000001 01 80 10 80 lo\n"
        )
        assert addressing.has_global_ipv6() is False

    def test_link_local_alone_is_not_a_route(self, monkeypatch, tmp_path):
        self._procfs(
            monkeypatch,
            tmp_path,
            "fe800000000000000042acfffe110002 27 40 20 80 eth0\n",
        )
        assert addressing.has_global_ipv6() is False

    def test_a_global_address_counts(self, monkeypatch, tmp_path):
        self._procfs(
            monkeypatch,
            tmp_path,
            "00000000000000000000000000000001 01 80 10 80 lo\n"
            "26000000000000000000000000000001 02 40 00 80 eth0\n",
        )
        assert addressing.has_global_ipv6() is True

    def test_a_platform_without_procfs_is_assumed_dual_stack(
        self, monkeypatch, tmp_path
    ):
        # Changing nothing is the right default off Linux: this is a targeted
        # fix, not a policy.
        monkeypatch.setattr(addressing, "_IF_INET6", tmp_path / "absent")
        assert addressing.has_global_ipv6() is True

    def test_a_malformed_line_is_skipped(self, monkeypatch, tmp_path):
        self._procfs(monkeypatch, tmp_path, "garbage\n\nnot-32-chars 01\n")
        assert addressing.has_global_ipv6() is False
