from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass


class NetGuardError(Exception):
    """Base class for every failure net_guard raises. Callers outside this module
    (worker.py) must catch this base class, never bare Exception, so unrelated bugs
    are never silently swallowed."""


class SSRFRejected(NetGuardError):
    """Raised when a URL, hostname, port, or a resolved IP address fails the SSRF
    policy. Never let this exception's message reach an end user -- it can describe
    internal network topology (which check fired, what address was rejected)."""


class FetchError(NetGuardError):
    """Raised by fetch() (added in a later step) for failures that happen only
    after a hop's target has already passed validation: too many redirects, an
    oversized body, a timeout, or an underlying transport/connection error."""


_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT_NETWORK = ipaddress.ip_network("::/96")


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


def _addr_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ip must never be connected to.

    ORDERING IS DELIBERATE, DO NOT "SIMPLIFY" IT: for an IPv6 address, the
    embedded-IPv4 unwrap (ipv4_mapped / sixtofour / teredo / NAT64 / deprecated
    ::-IPv4-compatible) runs UNCONDITIONALLY, before the generic not-is_global /
    multicast / reserved / unspecified check below it -- not after. Verified on the
    installed interpreter: ip.is_global already internally special-cases
    ipv4_mapped and blanket-excludes the ENTIRE teredo (2001::/23) and 6to4
    (2002::/16) prefixes via its own private-ranges table. If the generic check ran
    FIRST, those three unwrap branches would be short-circuited before ever
    executing for any input that would prove them correct -- a broken
    implementation (e.g. one that crashes on .teredo's 2-tuple return value, or
    silently no-ops on it) would still pass every test, because the outer check
    already returns True first. Running the unwrap first keeps that code live and
    genuinely exercised by tests. It does not change the final True/False verdict
    for any input (every branch is an independent sufficient condition, OR'd
    together) -- it only changes which branch fires first and what a test actually
    proves.

    Two branches ARE live, exploitable gaps on the installed interpreter (not just
    defense-in-depth), confirmed empirically: NAT64's well-known prefix
    64:ff9b::/96 (ipaddress.ip_address('64:ff9b::c0a8:701').is_global is True) and
    the deprecated IPv4-compatible ::/96 range (ipaddress.ip_address('::192.168.7.50')
    has .ipv4_mapped is None -- .ipv4_mapped only matches the DIFFERENT prefix
    ::ffff:0:0/96 -- AND .is_global is True). Both would silently pass a naive
    `not ip.is_global` check alone.

    NEVER use ip.is_private anywhere in this module: ipaddress.ip_address('100.64.0.1')
    (CGNAT space) has .is_private == False, so is_private would let CGNAT-routed
    traffic through. .is_global is the correct, and only correct, global-
    reachability check.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and _addr_is_forbidden(ip.ipv4_mapped):
            return True
        if ip.sixtofour is not None and _addr_is_forbidden(ip.sixtofour):
            return True
        if ip.teredo is not None:
            # .teredo returns a 2-TUPLE (server, client) of IPv4Address, unlike
            # .ipv4_mapped/.sixtofour which return a single IPv4Address or None --
            # passing the tuple whole to _addr_is_forbidden crashes or misbehaves.
            # The client half is where a hostile teredo address embeds its real
            # target; check both.
            server, client = ip.teredo
            if _addr_is_forbidden(server) or _addr_is_forbidden(client):
                return True
        if ip in _NAT64_NETWORK and _addr_is_forbidden(_embedded_v4(ip)):
            return True
        if ip in _IPV4_COMPAT_NETWORK and _addr_is_forbidden(_embedded_v4(ip)):
            return True
    if not ip.is_global:
        return True
    return bool(ip.is_multicast or ip.is_reserved or ip.is_unspecified)


_ALLOWED_PORTS = frozenset({80, 443})
_FORBIDDEN_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa")


@dataclass(frozen=True)
class ResolvedTarget:
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    host: str        # IDNA-encoded, lowercase, trailing-dot-stripped hostname (NOT the IP)
    port: int        # always 80 or 443
    scheme: str       # 'http' or 'https', lowercase
    path_qs: str      # e.g. '/a/b?x=1'; always starts with '/', never empty


def resolve_public_target(url: str) -> ResolvedTarget:
    """Validates url and resolves its hostname, returning a target safe to connect
    to. Raises SSRFRejected on any violation. Checks run cheapest/most
    request-independent first; the port check runs BEFORE DNS resolution --
    explicitly the single highest-value line in the whole guard per the design doc,
    since it neutralizes every non-80/443 internal admin port even if every IP
    check below it has a bug, and it must reject before any socket.getaddrinfo call
    is made."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFRejected(f"scheme {parts.scheme!r} is not allowed")
    if parts.username is not None or parts.password is not None:
        raise SSRFRejected("credentials embedded in the url are not allowed")

    hostname = parts.hostname
    if not hostname:
        raise SSRFRejected("url has no hostname")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SSRFRejected(f"hostname failed idna encoding: {exc}") from exc
    hostname = hostname.rstrip(".")
    if not hostname:
        raise SSRFRejected("url has no hostname")
    lowered = hostname.lower()
    if lowered == "localhost" or lowered.endswith(_FORBIDDEN_HOST_SUFFIXES):
        raise SSRFRejected(f"hostname {hostname!r} uses a disallowed local suffix")

    try:
        port = parts.port
    except ValueError as exc:
        raise SSRFRejected("invalid port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise SSRFRejected(f"port {port} is not allowed -- only 80/443")

    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFRejected(f"dns resolution failed for {hostname!r}: {exc}") from exc

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _socktype, _proto, _canonname, sockaddr in addr_infos:
        addr_str = sockaddr[0].split("%", 1)[0]  # strip a link-local zone id if present
        ip = ipaddress.ip_address(addr_str)
        if _addr_is_forbidden(ip):
            # Reject the WHOLE url immediately -- do not skip this address and try
            # another from the list. An attacker's DNS can return one public and
            # one private address; silently using the public one and ignoring the
            # private one is a DNS-rebinding invitation.
            raise SSRFRejected(f"resolved address for {hostname!r} is not a public address")
        resolved.append(ip)
    if not resolved:
        raise SSRFRejected(f"no addresses resolved for {hostname!r}")

    path_qs = parts.path or "/"
    if parts.query:
        path_qs += "?" + parts.query
    return ResolvedTarget(ip=resolved[0], host=hostname, port=port, scheme=scheme, path_qs=path_qs)
