"""SSRF protection for server-side URL fetches.

A tenant can hand us any URL and we fetch it from inside our own network, so an
unguarded fetch is a proxy into the private subnet: cloud metadata endpoints
(``169.254.169.254``), internal admin panels, the database host. This module is
the single place that decides whether a target is safe to request.

What it enforces:

* http/https only, and no credentials embedded in the URL.
* Every address the hostname resolves to must be publicly routable. Checking the
  hostname string is not enough — ``metadata.internal`` and a DNS record pointing
  at ``10.0.0.5`` both resolve into private space, so the check is on the
  resolved addresses, and *all* of them must pass (a multi-record host cannot
  smuggle one private address through).
* Only ports 80 and 443 unless the deployment widens ``URL_FETCH_ALLOWED_PORTS``.
* Redirects are not followed by the HTTP client. The caller walks the chain and
  validates every hop through :func:`assert_fetchable`, so a 302 into private
  space is rejected rather than followed.
* DNS rebinding: the name is resolved here and resolved *again* by the client
  when it connects, and a hostile resolver can answer differently the second
  time. :func:`assert_peer_public` closes that by asking the open connection
  which address it actually reached and rejecting the response if that address is
  private. The connection is established at that point but nothing has been read
  from it, so no internal response body ever reaches the caller. This is checked
  after connect rather than by pinning the IP because pinning breaks TLS
  verification and proxies; the post-connect check needs neither.

Development convenience: ``URL_FETCH_ALLOW_PRIVATE=true`` disables the address
checks so localhost fixtures can be ingested. ``validate_runtime()`` refuses that
combination in production.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_public(address: str) -> bool:
    """True only for a globally routable address.

    ``is_global`` is the authoritative flag; the explicit checks that follow are
    kept because they name the cases this guard exists to stop, which is what a
    reader needs to see.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — cloud metadata lives here
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # IPv6 mapped IPv4 (::ffff:10.0.0.1) would otherwise slip past the flags above
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public(str(ip.ipv4_mapped))
    return bool(getattr(ip, "is_global", True))


def assert_fetchable(
    url: str, *, allow_private: bool = False, allowed_ports: frozenset[int] | None = None
) -> None:
    """Raise ``ValidationError`` unless ``url`` is safe to request server-side.

    Call this before every request in a redirect chain, not just the first.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as exc:
        # a malformed IPv6 literal (`http://[::1`) makes urlparse itself raise
        # before any field is readable, and callers translate only
        # ValidationError into a fetch refusal
        raise ValidationError("url could not be parsed") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValidationError("url must be an http or https address")
    if not hostname:
        raise ValidationError("url has no host")
    # user:pass@host would leak credentials to whatever we resolve to, and is a
    # common way to make a hostile URL look like a trusted one
    if parsed.username or parsed.password:
        raise ValidationError("url must not contain credentials")

    try:
        port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    except ValueError as exc:
        # urlparse raises a bare ValueError for an out-of-range or non-numeric
        # port, and callers translate only ValidationError into a fetch refusal
        raise ValidationError("url port is not valid") from exc
    ports = allowed_ports if allowed_ports is not None else frozenset({80, 443})
    if port not in ports:
        raise ValidationError(f"url port {port} is not allowed")

    if allow_private:
        return

    try:
        # resolve once, check every answer: one private record is enough to reject
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationError("url host could not be resolved") from exc
    except OSError as exc:
        # getaddrinfo can raise other OSError subclasses (and UnicodeError from a
        # hostile hostname) that gaierror does not cover
        raise ValidationError("url host could not be resolved") from exc
    except UnicodeError as exc:
        raise ValidationError("url host could not be resolved") from exc

    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise ValidationError("url host could not be resolved")

    for address in addresses:
        if not _is_public(address):
            # the address is deliberately not echoed back: the response would
            # become an internal-network scanner
            logger.warning("ssrf_blocked", host=hostname, reason="non_public_address")
            raise ValidationError("url resolves to a non-public address")


def assert_peer_public(response: Any, *, allow_private: bool = False) -> None:
    """Reject a response whose connection actually landed on a private address.

    Call this on an open response before reading its body. ``assert_fetchable``
    validated the name's DNS answers; this validates the address the socket
    reached, which is the only thing that closes the gap between our resolution
    and the client's.

    The peer address is read from httpx's ``network_stream`` extension. If it is
    unavailable — a mock transport in a test, a future httpx that renames it — the
    check is skipped rather than failing the fetch: pre-connect validation still
    applies, and a hard failure here would break every non-network transport. That
    trade-off is why this is defence in depth and not the primary control.
    """
    if allow_private:
        return

    stream = response.extensions.get("network_stream") if response.extensions else None
    if stream is None:
        return
    try:
        peer = stream.get_extra_info("server_addr")
    except Exception:  # pragma: no cover - transport specific
        return
    if not peer:
        return

    address = peer[0] if isinstance(peer, (tuple, list)) else peer
    if not _is_public(str(address)):
        logger.warning("ssrf_blocked", reason="non_public_peer", url=str(response.url))
        raise ValidationError("url resolves to a non-public address")
