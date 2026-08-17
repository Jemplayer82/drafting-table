from __future__ import annotations

import http.server
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import pytest

import net_guard


class _QuietBaseHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class OkHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"hello from net_guard")
        else:
            self.send_error(404)


class RedirectOnceHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path == "/final":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"final destination")
        else:
            self.send_error(404)


class RedirectPrivateHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://192.168.1.1/secret")
        self.end_headers()


class SelfRedirectHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "/next")
        self.end_headers()


class OversizedHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"x" * 3_000_000)


class SlowHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/slow":
            time.sleep(0.5)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"slow response")
        else:
            self.send_error(404)


class SlowRedirectHandler(_QuietBaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/slow-redirect":
            time.sleep(0.3)
            self.send_response(302)
            self.send_header("Location", "/slow-final")
            self.end_headers()
        elif self.path == "/slow-final":
            time.sleep(0.3)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"slow final destination")
        else:
            self.send_error(404)


def test_rfc1918_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("192.168.1.1")) is True


def test_cgnat_rejected_despite_not_being_is_private() -> None:
    assert ipaddress.ip_address("100.64.0.1").is_private is False
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("100.64.0.1")) is True


def test_loopback_v4_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("127.0.0.1")) is True


def test_loopback_v6_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("::1")) is True


def test_link_local_v4_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("169.254.1.1")) is True


def test_link_local_v6_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("fe80::1")) is True


def test_multicast_v4_rejected() -> None:
    assert ipaddress.ip_address("224.0.0.1").is_global is True
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("224.0.0.1")) is True


def test_multicast_v6_rejected() -> None:
    assert ipaddress.ip_address("ff02::1").is_global is True
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("ff02::1")) is True


def test_unspecified_v4_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("0.0.0.0")) is True


def test_unspecified_v6_rejected() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("::")) is True


def test_nat64_embedded_private_address_rejected() -> None:
    ip = ipaddress.ip_address("64:ff9b::c0a8:701")
    assert ip.is_global is True
    assert net_guard._addr_is_forbidden(ip) is True


def test_ipv4_compatible_ipv6_rejected_despite_ipv4_mapped_being_none() -> None:
    ip = ipaddress.ip_address("::192.168.7.50")
    assert ip.ipv4_mapped is None
    assert ip.is_global is True
    assert net_guard._addr_is_forbidden(ip) is True


def test_teredo_tuple_unpacked_without_crashing_and_rejects_private_client() -> None:
    ip = ipaddress.ip_address("2001::3f57:fefe")
    assert ip.teredo == (
        ipaddress.IPv4Address("0.0.0.0"),
        ipaddress.IPv4Address("192.168.1.1"),
    )
    # On this interpreter the whole 2001::/23 block is already caught by the plain
    # is_global check too, so this test's real value is proving the 2-tuple unpack
    # doesn't crash or get silently ignored, not that the recursion changes the verdict.
    assert net_guard._addr_is_forbidden(ip) is True


def test_sixtofour_embedded_private_address_rejected() -> None:
    ip = ipaddress.ip_address("2002:c0a8:101::")
    assert ip.sixtofour == ipaddress.IPv4Address("192.168.1.1")
    assert net_guard._addr_is_forbidden(ip) is True


def test_public_ip_v4_accepted() -> None:
    assert net_guard._addr_is_forbidden(ipaddress.ip_address("8.8.8.8")) is False


def test_public_ip_v6_accepted() -> None:
    assert (
        net_guard._addr_is_forbidden(ipaddress.ip_address("2606:4700:4700::1111")) is False
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "ftp://example.com/",
    ],
)
def test_non_http_scheme_rejected(url: str) -> None:
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.resolve_public_target(url)


def test_credentials_in_url_rejected() -> None:
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.resolve_public_target("http://user:pass@example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://foo.local/",
        "http://foo.localhost/",
        "http://foo.internal/",
        "http://foo.home.arpa/",
    ],
)
def test_localhost_and_local_suffixes_rejected(url: str) -> None:
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.resolve_public_target(url)


def test_disallowed_port_rejected_before_dns_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("getaddrinfo must not be called before the port check")
        ),
    )
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.resolve_public_target("http://obviously-fake-nonexistent-host.invalid:9443/")


def test_default_port_applied_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
        ],
    )
    assert net_guard.resolve_public_target("https://example.test/").port == 443
    assert net_guard.resolve_public_target("http://example.test/").port == 80


def test_resolve_rejects_whole_url_when_any_resolved_address_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 0)),
        ],
    )
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.resolve_public_target("http://example.test/")


def test_resolve_returns_first_resolved_address_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
        ],
    )
    target = net_guard.resolve_public_target("http://example.test/")
    assert target.ip == ipaddress.ip_address("1.1.1.1")


def test_resolve_accepts_real_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        net_guard.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
        ],
    )
    target = net_guard.resolve_public_target("https://ok.test/path?x=1")
    assert target.ip == ipaddress.ip_address("93.184.216.34")
    assert target.host == "ok.test"
    assert target.port == 443
    assert target.scheme == "https"
    assert target.path_qs == "/path?x=1"


def test_ipv6_literal_url_round_trips() -> None:
    target = net_guard.resolve_public_target("http://[2606:4700:4700::1111]/")
    assert target.ip == ipaddress.ip_address("2606:4700:4700::1111")


def test_fetch_returns_response_for_valid_local_target(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(OkHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    result = net_guard.fetch(base_url + "/ok")
    assert result.status_code == 200
    assert result.body == b"hello from net_guard"
    assert result.final_url == base_url + "/ok"


def test_fetch_follows_redirect_within_limit_and_returns_final_body(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(RedirectOnceHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    result = net_guard.fetch(base_url + "/redirect-once")
    assert result.status_code == 200
    assert result.body == b"final destination"
    assert result.final_url == base_url + "/final"


def test_fetch_rejects_redirect_chain_longer_than_max_redirects(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(SelfRedirectHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    with pytest.raises(net_guard.FetchError):
        net_guard.fetch(base_url + "/next", max_redirects=2)


def test_fetch_rejects_redirect_to_private_ip(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(RedirectPrivateHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    with pytest.raises(net_guard.SSRFRejected):
        net_guard.fetch(base_url + "/redirect-private")


def test_fetch_rejects_body_larger_than_max_bytes(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(OversizedHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    with pytest.raises(net_guard.FetchError):
        net_guard.fetch(base_url + "/big", max_bytes=1000)


def test_fetch_raises_fetch_error_when_wall_clock_timeout_exceeded(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(SlowHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    with pytest.raises(net_guard.FetchError):
        net_guard.fetch(base_url + "/slow", timeout=0.1)


def test_fetch_threads_remaining_timeout_across_redirects(
    local_http_server, guard_allow_loopback
) -> None:
    base_url = local_http_server(SlowRedirectHandler)
    port = urlsplit(base_url).port
    guard_allow_loopback(port)
    with pytest.raises(net_guard.FetchError):
        net_guard.fetch(base_url + "/slow-redirect", timeout=0.4)


def test_fetch_wraps_connection_refused_as_fetch_error(guard_allow_loopback) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    guard_allow_loopback(port)
    with pytest.raises(net_guard.FetchError):
        net_guard.fetch(f"http://127.0.0.1:{port}/")
