"""Choosing target addresses this machine can actually reach.

A managed Postgres hostname resolves to several addresses, and Neon publishes
both A and AAAA records. Inside a container with no IPv6 route every AAAA is
dead weight: the connection fails with "Network is unreachable" against an
address that was never reachable from here.

That alone would be survivable, because libpq tries each address in turn and
the IPv4 ones succeed. What is not survivable is the resolver intermittently
returning *only* the AAAA records -- observed repeatedly on Docker's embedded
resolver under WSL2, where a query returns both families on one call and IPv6
only on the next. Then every attempt fails and the connection is reported dead
while the server is sitting there answering on IPv4.

So the family is chosen here rather than left to chance:

* If this machine has a global IPv6 address, nothing is filtered. A dual-stack
  host should keep using whatever the resolver prefers.
* If it does not, IPv6 results are dropped, and if that leaves nothing, the
  name is resolved again asking specifically for A records. The second lookup
  is the one that fixes the intermittent case: the A records exist, the
  resolver simply did not volunteer them.

The addresses are handed to libpq as ``hostaddr`` alongside the original
``host``, so TLS still verifies against the name and every address is still
tried in turn. The hostname is what the certificate is issued for; connecting
by literal address alone would break ``verify-full`` and Neon's SNI routing.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Linux exposes every configured IPv6 address here, one per line.
_IF_INET6 = Path("/proc/net/if_inet6")


def _is_usable_global(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    # Loopback (::1) and link-local (fe80::/10) prove nothing about whether a
    # public address is reachable; a container with only those cannot route to
    # the internet over IPv6.
    return not (parsed.is_loopback or parsed.is_link_local)


@lru_cache(maxsize=1)
def has_global_ipv6() -> bool:
    """Whether this machine holds an IPv6 address that could reach the internet.

    Read from the kernel rather than probed with a connection: a probe costs a
    timeout on exactly the broken network this exists to detect. Cached because
    the answer cannot change without the process being restarted in any
    deployment this service supports -- a container's addressing is fixed at
    start.

    Anything other than Linux is assumed dual-stack, which means "change
    nothing": this is a targeted fix for a known container condition, not a
    policy about address families.
    """
    if not _IF_INET6.exists():
        return True

    try:
        lines = _IF_INET6.read_text().splitlines()
    except OSError:  # pragma: no cover - unreadable procfs
        return True

    for line in lines:
        raw = line.split()
        if not raw:
            continue
        # 32 hex chars, no colons. Reinsert them to parse.
        packed = raw[0]
        if len(packed) != 32:
            continue
        address = ":".join(packed[index : index + 4] for index in range(0, 32, 4))
        if _is_usable_global(address):
            return True
    return False


def _resolve(host: str, port: int, family: int) -> list[str]:
    try:
        results = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except OSError as exc:
        logger.debug("resolving %s failed: %s", host, exc)
        return []
    # Ordered, deduplicated: getaddrinfo returns one entry per socket type, and
    # the order it gives is the order libpq should try.
    seen: dict[str, None] = {}
    for entry in results:
        seen.setdefault(entry[4][0], None)
    return list(seen)


@lru_cache(maxsize=256)
def routable_addresses(host: str, port: int) -> tuple[str, ...]:
    """Addresses for ``host`` that this machine has any hope of reaching.

    Returns an empty tuple to mean "no opinion" -- an IP literal was given, the
    name did not resolve, or this machine is dual-stack. The caller then leaves
    resolution to libpq exactly as before.

    Cached, and returning a tuple so it can be: this runs while building a
    target engine, and engines are themselves cached, but a saved connection
    being re-tested should not pay a fresh lookup every time. Call
    ``routable_addresses.cache_clear()`` if a target's DNS changes under a
    long-running process.
    """
    try:
        ipaddress.ip_address(host)
        return ()  # already an address; nothing to choose between
    except ValueError:
        pass

    if has_global_ipv6():
        return ()

    addresses = _resolve(host, port, socket.AF_UNSPEC)
    usable = tuple(a for a in addresses if ":" not in a)
    if usable:
        return usable

    # Either nothing resolved, or everything that resolved was IPv6 we cannot
    # route. The second case is the intermittent one worth a second lookup:
    # asking for A records specifically returns them when AF_UNSPEC did not.
    if addresses:
        logger.info(
            "%s resolved to IPv6 only and this host has no IPv6 route; "
            "asking for A records specifically",
            host,
        )
    return tuple(_resolve(host, port, socket.AF_INET))
