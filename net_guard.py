from __future__ import annotations

import ipaddress


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
