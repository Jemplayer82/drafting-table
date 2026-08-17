from __future__ import annotations

import http.server
import ipaddress
import threading
from pathlib import Path

import pytest

import net_guard


@pytest.fixture()
def app_env(monkeypatch, tmp_path: Path):
    """Set up an isolated env + temp DB dir before app.py is imported."""
    from auth import hash_password

    monkeypatch.setenv("ADMIN_USER", "captain")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("test-password-please-1234"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-prod")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    yield


@pytest.fixture()
def client(app_env):
    import importlib

    import db as db_module

    importlib.reload(db_module)
    import app as app_module

    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def logged_in_client(client):
    resp = client.post(
        "/login", data={"username": "captain", "password": "test-password-please-1234"}
    )
    assert resp.status_code == 302
    return client


@pytest.fixture()
def local_http_server():
    """Factory: local_http_server(handler_cls) starts a ThreadingHTTPServer bound to
    127.0.0.1 on an OS-assigned ephemeral port using the given
    http.server.BaseHTTPRequestHandler subclass, and returns its base url
    ('http://127.0.0.1:<port>'). Every server started during the test is shut down
    automatically at teardown. Each test defines its own handler_cls (overriding
    do_GET) to control exactly what routes/status codes/headers/bodies it serves."""
    servers = []

    def _make(handler_cls) -> str:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _make

    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def guard_allow_loopback(monkeypatch):
    """Returns allow(port): patches net_guard so 127.0.0.1 is treated as a public
    address and `port` is added to the allowed-port set, while leaving every other
    part of the real guard logic untouched -- DNS resolution, hostname/scheme/
    credential checks, and forbidden-address checks for every OTHER address
    (including redirect targets) all still run for real. The real guard would
    otherwise correctly refuse to fetch a local test server on BOTH the loopback-
    address check and the 80/443 port allowlist (test servers bind an ephemeral,
    non-standard port); this narrowly punches through just enough to let tests
    exercise real DNS/redirect-revalidation logic against a real local server
    without weakening the guard for anything except 127.0.0.1 itself on the one
    port the test explicitly allowed."""

    def _allow(port: int) -> None:
        real_forbidden = net_guard._addr_is_forbidden
        loopback = ipaddress.ip_address("127.0.0.1")

        def patched(ip):
            if ip == loopback:
                return False
            return real_forbidden(ip)

        monkeypatch.setattr(net_guard, "_addr_is_forbidden", patched)
        monkeypatch.setattr(net_guard, "_ALLOWED_PORTS", net_guard._ALLOWED_PORTS | {port})

    return _allow
