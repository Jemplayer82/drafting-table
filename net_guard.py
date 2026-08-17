"""SSRF-hardened network utilities for the worker ingest pipeline.

This module validates outbound URLs (scheme, DNS resolution, public IP
constraints) and performs fetches with a bounded wall-clock budget.
"""

import dataclasses
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse, urljoin

import httpx


class NetGuardError(Exception):
    """Base class for all net_guard failures."""
    pass


class SSRFRejected(NetGuardError):
    """Raised when a URL or its resolved address fails SSRF validation."""
    pass


class FetchError(NetGuardError):
    """Raised when the HTTP fetch itself fails."""
    pass


_MAX_REDIRECTS = 10
_DEFAULT_FETCH_TIMEOUT = 30.0
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DNS_RESOLUTION_TIMEOUT = 10.0

_ALLOWED_SCHEMES = frozenset(("http", "https"))
_ALLOWED_PORTS = frozenset((80, 443))

_FORBIDDEN_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".home.arpa",
)

_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT_NETWORK = ipaddress.ip_network("::/96")


@dataclasses.dataclass(frozen=True)
class ResolvedTarget:
    """A URL target whose hostname has been resolved to a concrete public IP."""
    scheme: str
    host: str
    port: int
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    path_qs: str

    @property
    def host_header(self) -> str:
        if self.ip.version == 6:
            return f"[{self.host}]"
        return self.host

    @property
    def url(self) -> str:
        if self.ip.version == 6:
            netloc = f"[{self.ip}]:{self.port}"
        else:
            netloc = f"{self.ip}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path_qs}"

    def __str__(self) -> str:
        return self.url


@dataclasses.dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: bytes
    headers: dict
    final_url: str


def _addr_is_forbidden(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* is not a publicly routable IP address."""
    if addr.version == 4:
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return True
        if addr in _CGNAT_NETWORK:
            return True
        return False

    # IPv6
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True

    if addr.ipv4_mapped is not None and _addr_is_forbidden(addr.ipv4_mapped):
        return True

    if addr in _NAT64_NETWORK and _addr_is_forbidden(_embedded_v4(addr)):
        return True

    if addr in _IPV4_COMPAT_NETWORK and _addr_is_forbidden(_embedded_v4(addr)):
        return True

    if addr.sixtofour is not None and _addr_is_forbidden(addr.sixtofour):
        return True

    if addr.teredo is not None:
        server, client = addr.teredo
        if _addr_is_forbidden(server) or _addr_is_forbidden(client):
            return True

    return False


def _embedded_v4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address:
    """Extract the IPv4 address encoded in the low 32 bits of *addr*."""
    return ipaddress.IPv4Address(int.from_bytes(addr.packed[-4:], "big"))


def _resolve_with_timeout(hostname: str, port: int):
    """Resolve *hostname*/*port* with a hard timeout.

    socket.getaddrinfo() has no timeout argument and may block for a very long
    time if an authoritative nameserver drops queries.  We run the call in a
    daemon thread and abandon it on timeout so the caller can never be blocked
    indefinitely by one hostile DNS server.
    """
    result = [None]
    error = [None]

    def _resolve():
        try:
            result[0] = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except Exception as exc:  # noqa: BLE001
            error[0] = exc

    resolver_thread = threading.Thread(target=_resolve, daemon=True)
    resolver_thread.start()
    resolver_thread.join(_DNS_RESOLUTION_TIMEOUT)

    if resolver_thread.is_alive():
        raise SSRFRejected("DNS resolution timed out")

    if error[0] is not None:
        raise error[0]

    if result[0] is None:
        raise SSRFRejected("DNS resolution returned no results")

    return result[0]


def resolve_public_target(url: str) -> ResolvedTarget:
    """Validate *url* and return a ResolvedTarget safe for fetch().

    The returned target contains the first resolved public IP while preserving
    the original hostname for the HTTP Host header / TLS SNI.  The DNS lookup
    itself is capped by _DNS_RESOLUTION_TIMEOUT so it cannot stall the worker.
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFRejected("Only http and https URLs are allowed")

    if parsed.username is not None or parsed.password is not None:
        raise SSRFRejected("URL must not contain credentials")

    if not parsed.hostname:
        raise SSRFRejected("URL has no hostname")

    hostname = parsed.hostname.lower()

    for suffix in _FORBIDDEN_HOST_SUFFIXES:
        bare = suffix.lstrip(".")
        if hostname == bare or hostname.endswith(suffix):
            raise SSRFRejected("URL hostname is not allowed")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFRejected("Invalid URL port") from exc

    if port not in _ALLOWED_PORTS:
        raise SSRFRejected("URL port is not allowed")

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SSRFRejected("URL hostname is not valid") from exc

    path_qs = parsed.path or ""
    if parsed.query:
        path_qs += "?" + parsed.query

    try:
        ip = ipaddress.ip_address(ascii_host)
    except ValueError:
        ip = None

    if ip is not None:
        if _addr_is_forbidden(ip):
            raise SSRFRejected("URL resolves to a non-public address")
        return ResolvedTarget(
            scheme=parsed.scheme,
            host=hostname,
            port=port,
            ip=ip,
            path_qs=path_qs,
        )

    try:
        addrinfo = _resolve_with_timeout(ascii_host, port)
    except socket.gaierror as exc:
        raise SSRFRejected("Could not resolve URL hostname") from exc

    if not addrinfo:
        raise SSRFRejected("Could not resolve URL hostname")

    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        resolved_ip = ipaddress.ip_address(sockaddr[0])
        if _addr_is_forbidden(resolved_ip):
            raise SSRFRejected("URL resolves to a non-public address")

    first_family, _socktype, _proto, _canonname, first_sockaddr = addrinfo[0]
    first_ip = ipaddress.ip_address(first_sockaddr[0])

    return ResolvedTarget(
        scheme=parsed.scheme,
        host=hostname,
        port=port,
        ip=first_ip,
        path_qs=path_qs,
    )


def _hop_url(base_url: str, location: str) -> str:
    """Resolve a redirect Location header relative to *base_url*."""
    return urljoin(base_url, location)


def _read_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read *response* into memory, raising if it exceeds *max_bytes*."""
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise FetchError("Response body exceeds maximum allowed size")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_FETCH_TIMEOUT,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Fetch *url* safely, following redirects manually.

    ``timeout`` is a wall-clock budget covering the WHOLE fetch, including
    every redirect hop.  Within each hop the actual HTTP stream is bounded by
    the remaining budget via ``client.stream(..., timeout=remaining)``.

    DNS resolution of each hop (performed by :func:`resolve_public_target`) is
    capped by the fixed module-level ``_DNS_RESOLUTION_TIMEOUT`` constant.  It is
    therefore not charged against ``remaining`` to the second, but it can never
    block the caller indefinitely.
    """
    close_client = client is None
    if close_client:
        client = httpx.Client(follow_redirects=False)

    deadline = time.monotonic() + timeout
    current_url = url
    redirects_remaining = max_redirects

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("Fetch wall-clock budget exhausted")

            # Must remain a single-argument call so tests that monkeypatch
            # resolve_public_target with a one-arg lambda keep working.
            target = resolve_public_target(current_url)

            try:
                with client.stream(
                    "GET",
                    target.url,
                    headers={"Host": target.host_header},
                    follow_redirects=False,
                    timeout=remaining,
                    extensions={"sni_hostname": target.host},
                ) as response:
                    body = _read_body(response, max_bytes)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FetchError("Could not fetch URL") from exc

            if response.status_code not in (301, 302, 303, 307, 308):
                return FetchResult(
                    status_code=response.status_code,
                    body=body,
                    headers=dict(response.headers),
                    final_url=current_url,
                )

            if redirects_remaining <= 0:
                raise FetchError("Too many redirects")

            redirects_remaining -= 1
            location = response.headers.get("location")
            if not location:
                raise FetchError("Redirect response missing Location header")

            current_url = _hop_url(current_url, location)

    finally:
        if close_client:
            client.close()
