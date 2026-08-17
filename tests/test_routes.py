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


def test_board_shows_real_cover_for_project_with_images(logged_in_client):
    resp = logged_in_client.get("/")
    assert resp.status_code == 200
    assert b"/media/" in resp.data
    assert b"dt-board__cover--empty" in resp.data


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


def test_media_requires_auth(client):
    resp = client.get("/media/" + "0" * 32)
    assert resp.status_code == 401


def test_media_rejects_non_hex_id(logged_in_client):
    resp = logged_in_client.get("/media/" + "z" * 32)
    assert resp.status_code == 404


def test_media_rejects_wrong_length_id(logged_in_client):
    resp = logged_in_client.get("/media/deadbeef")
    assert resp.status_code == 404


def test_media_rejects_traversal_id(logged_in_client):
    resp = logged_in_client.get("/media/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    assert resp.status_code == 404


def test_media_serves_seeded_thumbnail(logged_in_client):
    import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT thumb_media_id FROM items WHERE thumb_media_id IS NOT NULL LIMIT 1"
        ).fetchone()
    assert row is not None
    resp = logged_in_client.get(f"/media/{row['thumb_media_id']}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/webp")
    assert "inline" in resp.headers["Content-Disposition"]
    assert "filename=" in resp.headers["Content-Disposition"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"


def test_project_page_requires_auth(client):
    resp = client.get("/p/jemplayer82-web-design-ideas", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_project_page_404s_for_unknown_slug(logged_in_client):
    resp = logged_in_client.get("/p/does-not-exist")
    assert resp.status_code == 404


def test_project_page_renders_all_18_real_items(logged_in_client):
    resp = logged_in_client.get("/p/jemplayer82-web-design-ideas")
    assert resp.status_code == 200
    assert b"MNTN" in resp.data
    assert b"layout + type" in resp.data
    assert b"/media/" in resp.data
    assert b"open questions" in resp.data


def test_project_page_renders_swatches_and_synthesis(logged_in_client):
    resp = logged_in_client.get("/p/jemplayer82-web-design-ideas")
    assert resp.status_code == 200
    assert b"#D5891B" in resp.data
    assert b"palette-hunting" in resp.data
    assert b"Which single accent color" in resp.data
    assert b"no decisions logged yet." in resp.data


def test_project_page_swatches_use_nonce_style_block_not_inline_styles(logged_in_client):
    resp = logged_in_client.get("/p/jemplayer82-web-design-ideas")
    assert resp.status_code == 200

    # The CSP header must authorize the style block's nonce.
    csp = resp.headers["Content-Security-Policy"]
    csp_nonce_match = re.search(r"style-src [^;]*'nonce-([^']+)'", csp)
    assert csp_nonce_match, "expected a nonce in the CSP style-src directive"
    expected_nonce = csp_nonce_match.group(1)

    # Find the nonce'd <style> block and the rule that paints the known swatch.
    style_match = re.search(
        rb'<style nonce="([^"]+)">\s*(.*?)\s*</style>',
        resp.data,
        re.DOTALL,
    )
    assert style_match, "expected a nonce'd <style> block"
    style_nonce = style_match.group(1).decode()
    style_block = style_match.group(2)
    assert style_nonce == expected_nonce, "style block nonce must match CSP header nonce"

    rule_match = re.search(
        rb'\[data-sw="([^"]+)"\]\s*\{\s*background-color:\s*#D5891B\s*;?\s*\}',
        style_block,
    )
    assert rule_match, "expected a CSS rule setting #D5891B via a data-sw selector"
    data_sw_value = rule_match.group(1).decode()

    # The matching span must carry the same data-sw attribute.
    span_re = re.compile(rb'<span[^>]*class="dt-swatch"[^>]*>', re.IGNORECASE)
    swatch_spans = list(span_re.finditer(resp.data))
    assert any(
        f'data-sw="{data_sw_value}"'.encode() in span.group(0)
        for span in swatch_spans
    ), "expected a dt-swatch span with the matching data-sw attribute"

    # Positively rule out the old inline-style bug.
    for span in swatch_spans:
        tag = span.group(0).lower()
        assert not (
            b"style=" in tag and b"background-color" in tag
        ), "dt-swatch spans must not use inline style for background-color"


def test_project_page_example_project_renders_decisions(logged_in_client):
    resp = logged_in_client.get("/p/studio-portfolio-site")
    assert resp.status_code == 200
    assert b"Type" in resp.data
