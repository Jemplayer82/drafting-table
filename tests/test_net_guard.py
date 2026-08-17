from __future__ import annotations

import ipaddress
import socket

import pytest

import net_guard


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
