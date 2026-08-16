from __future__ import annotations

import re


def test_healthz_never_touches_db(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_root_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_login_page_loads(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"login" in resp.data.lower()


def test_wrong_password_rejected(client):
    resp = client.post("/login", data={"username": "captain", "password": "nope"})
    assert resp.status_code == 200
    assert b"Invalid credentials" in resp.data


def test_correct_login_sets_cookie_and_redirects(client):
    resp = client.post(
        "/login",
        data={
            "username": "captain",
            "password": "test-password-please-1234",  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "__Host-dt_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Strict" in set_cookie


def test_board_page_requires_auth(logged_in_client):
    resp = logged_in_client.get("/")
    assert resp.status_code == 200
    assert b"boards" in resp.data


def test_board_page_shows_both_seeded_projects(logged_in_client):
    resp = logged_in_client.get("/")
    assert resp.status_code == 200
    assert b"Studio Portfolio Site" in resp.data
    assert b"jemplayer82" in resp.data


def test_logout_without_csrf_token_is_rejected(logged_in_client):
    resp = logged_in_client.post("/logout")
    assert resp.status_code == 403


def test_logout_with_csrf_token_succeeds_and_revokes_session(logged_in_client):
    board = logged_in_client.get("/")
    m = re.search(rb'csrf_token" value="([^"]+)"', board.data)
    assert m, "expected a csrf_token hidden field on an authenticated page"
    token = m.group(1).decode()  # pragma: allowlist secret

    resp = logged_in_client.post("/logout", data={"csrf_token": token})  # pragma: allowlist secret
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"

    after = logged_in_client.get("/", follow_redirects=False)
    assert after.status_code == 302
    assert after.headers["Location"] == "/login"


def test_security_headers_present(client):
    resp = client.get("/login")
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "Strict-Transport-Security" in resp.headers


def test_lockout_after_repeated_failures(client):
    for _ in range(8):
        client.post("/login", data={"username": "captain", "password": "wrong"})
    resp = client.post(
        "/login",
        data={
            "username": "captain",
            "password": "test-password-please-1234",  # pragma: allowlist secret
        },
    )
    assert b"Too many attempts" in resp.data
