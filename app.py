from __future__ import annotations

import json
import os
import re
import secrets
from typing import Any

from flask import Flask, Response, g, redirect, render_template, request, url_for

import auth
import db
import seed


def _load_secret_from_file_or_env(var: str) -> None:
    """Support `<VAR>_FILE=/path` pointing at a mounted secret file, so production
    doesn't have to pass raw secrets through Portainer's stack env (that API has
    no auth on this deploy host -- see the plan doc). Falls back to `<VAR>` as-is
    for local dev via .env."""
    file_path = os.environ.get(f"{var}_FILE")
    if file_path:
        os.environ[var] = open(file_path, encoding="utf-8").read().strip()


for _var in ("ADMIN_PASSWORD_HASH", "SESSION_SECRET", "CLAUDE_CODE_OAUTH_TOKEN"):
    _load_secret_from_file_or_env(_var)

if not auth.admin_configured():
    raise RuntimeError(
        "ADMIN_USER and ADMIN_PASSWORD_HASH must both be set -- this app refuses to "
        "start without them rather than offering a first-run setup flow, because on "
        "a public internet endpoint that flow is a land-grab (whoever hits it first "
        "owns the account). Generate a hash with:\n\n"
        "    uv run python -m hashpw\n"
    )
if not os.environ.get("SESSION_SECRET"):
    raise RuntimeError(
        "SESSION_SECRET must be set. Generate one with:\n\n"
        '    python -c "import secrets; print(secrets.token_hex(32))"\n'
    )

db.init_db()
seed.run_seed_if_empty()

app = Flask(__name__)

_PUBLIC_PATHS = {"/login", "/healthz"}
_MEDIA_ID_RE = re.compile(r"[0-9a-f]{32}")
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_MEDIA_EXT_BY_MIME = {"image/jpeg": "jpg", "image/webp": "webp", "image/png": "png"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


def _client_ip() -> str:
    """Only trust X-Real-IP when the direct peer is the reverse proxy -- anyone on
    the LAN can otherwise spoof it to evade the login lockout."""
    trusted_proxy = os.environ.get("TRUSTED_PROXY_IP")
    if trusted_proxy and request.remote_addr == trusted_proxy:
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
    return request.remote_addr or "unknown"


@app.before_request
def _auth_gate() -> Any:
    if _is_public(request.path):
        return None

    session = auth.get_session(request.cookies.get(auth.COOKIE_NAME))
    if session is None:
        if request.path.startswith("/api/") or request.path.startswith("/media/"):
            return {"error": "unauthorized"}, 401
        return redirect(url_for("login"))
    g.session = session

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sec_fetch_site = request.headers.get("Sec-Fetch-Site")
        if sec_fetch_site is not None and sec_fetch_site not in ("same-origin", "none"):
            return {"error": "csrf_rejected"}, 403
        token = (
            request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        )
        if not auth.verify_csrf(session, token):
            return {"error": "csrf_rejected"}, 403

    return None


@app.after_request
def _security_headers(response):  # noqa: ANN001, ANN201
    nonce = getattr(g, "csp_nonce", "")
    if request.path.startswith("/media/"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "img-src 'self' data:; "
            f"style-src 'self' 'nonce-{nonce}'; style-src-attr 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), camera=(), microphone=(), interest-cohort=()"
    )
    return response


@app.before_request
def _csp_nonce() -> None:
    g.csp_nonce = secrets.token_urlsafe(16)


@app.route("/healthz")
def healthz():  # noqa: ANN201
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/login", methods=["GET", "POST"])
def login():  # noqa: ANN201
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")  # pragma: allowlist secret
        ip = _client_ip()

        if auth.is_locked_out(username, ip):
            error = "Too many attempts. Try again later."
        else:
            ok = auth.check_login(username, password)
            auth.record_login_attempt(username, ip, ok)
            if ok:
                session_id, _csrf = auth.create_session(username)
                resp = redirect(url_for("board"))
                resp.set_cookie(
                    auth.COOKIE_NAME,
                    session_id,
                    httponly=True,
                    secure=True,
                    samesite="Strict",
                    path="/",
                    max_age=auth.SESSION_HOURS * 3600,
                )
                return resp
            error = "Invalid credentials."

    return render_template("login.html", error=error, nonce=g.csp_nonce)


@app.route("/logout", methods=["POST"])
def logout():  # noqa: ANN201
    session = getattr(g, "session", None)
    if session:
        auth.revoke_session(session["id"])
    resp = redirect(url_for("login"))
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.route("/")
def board():  # noqa: ANN201
    with db.connect() as conn:
        projects = conn.execute(
            "SELECT id, name, slug FROM projects WHERE archived_at IS NULL ORDER BY updated_at DESC"
        ).fetchall()
        covers = {}
        for p in projects:
            cover = conn.execute(
                "SELECT thumb_media_id FROM items WHERE project_id = ? "
                "AND thumb_media_id IS NOT NULL ORDER BY position, id LIMIT 1",
                (p["id"],),
            ).fetchone()
            if cover:
                covers[p["id"]] = cover["thumb_media_id"]
    return render_template("board.html", projects=projects, covers=covers, nonce=g.csp_nonce)


@app.route("/media/<mid>")
def media(mid):  # noqa: ANN001, ANN201
    if not _MEDIA_ID_RE.fullmatch(mid):
        return "", 404
    with db.connect() as conn:
        row = conn.execute("SELECT path, mime FROM media WHERE id = ?", (mid,)).fetchone()
    if row is None:
        return "", 404
    media_path = db.MEDIA_DIR / row["path"]
    if not media_path.is_file():
        return "", 404
    ext = _MEDIA_EXT_BY_MIME.get(row["mime"], "bin")
    resp = Response(media_path.read_bytes(), mimetype=row["mime"])
    resp.headers["Content-Disposition"] = f'inline; filename="media.{ext}"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _render_project(
    slug: str,
    *,
    open_item_id: int | None = None,
    item_error: str | None = None,
    decision_error: str | None = None,
) -> str | None:
    """Shared by GET /p/<slug> and every mutating route's validation-failure path.
    Returns None if slug doesn't resolve to a non-archived project (caller 404s).
    open_item_id/item_error/decision_error are consumed by project.html markup added
    in later steps -- this step only threads them through render_template so those
    steps never need to touch this function's signature again."""
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id, name, slug, note FROM projects WHERE slug = ? AND archived_at IS NULL",
            (slug,),
        ).fetchone()
        if proj is None:
            return None
        items = conn.execute(
            "SELECT id, kind, status, source_url, title, tag, note_md, alt_text, "
            "thumb_media_id, thumb_w, thumb_h, position FROM items "
            "WHERE project_id = ? ORDER BY position, id",
            (proj["id"],),
        ).fetchall()
        swatch_rows = []
        if items:
            item_ids = [i["id"] for i in items]
            placeholders = ",".join("?" * len(item_ids))
            swatch_rows = conn.execute(
                f"SELECT item_id, hex, label FROM swatches WHERE item_id IN ({placeholders}) "
                "ORDER BY item_id, position",
                item_ids,
            ).fetchall()
        synthesis = conn.execute(
            "SELECT direction_md, questions_json FROM syntheses "
            "WHERE project_id = ? ORDER BY version DESC LIMIT 1",
            (proj["id"],),
        ).fetchone()
        decision_rows = conn.execute(
            "SELECT body_md, rationale_md FROM decisions "
            "WHERE project_id = ? AND status = 'accepted' ORDER BY created_at",
            (proj["id"],),
        ).fetchall()

    swatches_by_item = {}
    for row in swatch_rows:
        # Defense-in-depth: skip any hex that fails validation, even though
        # seed.py already validated on the way in.
        if not _HEX_RE.fullmatch(row["hex"] or ""):
            continue
        swatch = {"hex": row["hex"], "label": row["label"]}
        swatches_by_item.setdefault(row["item_id"], []).append(swatch)
    view_items = [
        {**dict(item), "swatches": swatches_by_item.get(item["id"], [])}
        for item in items
    ]

    direction_lines = []
    if synthesis:
        for line in (synthesis["direction_md"] or "").split("\n"):
            stripped = line.strip()
            if stripped:
                direction_lines.append(stripped[2:] if stripped.startswith("- ") else stripped)
    try:
        questions = json.loads(synthesis["questions_json"]) if synthesis else []
    except (json.JSONDecodeError, TypeError):
        questions = []

    decisions_view = []
    for row in decision_rows:
        label = row["rationale_md"].strip("*") if row["rationale_md"] else None
        decisions_view.append({"label": label, "body_md": row["body_md"]})

    return render_template(
        "project.html", project=proj, items=view_items, synthesis=synthesis,
        direction_lines=direction_lines, questions=questions,
        decisions=decisions_view, nonce=g.csp_nonce,
        open_item_id=open_item_id, item_error=item_error, decision_error=decision_error,
    )


@app.route("/p/<slug>")
def project_detail(slug):  # noqa: ANN001, ANN201
    html = _render_project(slug)
    if html is None:
        return "", 404
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DT_PORT", "8093")))
