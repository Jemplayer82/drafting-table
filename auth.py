"""Single-admin auth: DB-backed sessions (real logout/revocation), PBKDF2 with the
iteration count embedded in the hash, and a lockout check that runs BEFORE hashing
so the login route can't be turned into an unauthenticated CPU amplifier.

Shape lifted from dibbs-needle/web/auth.py (hash format, fail-closed check_login,
constant-time username compare) with three changes from TradingAgents/web/auth_app.py:
600k iterations instead of 200k, DB sessions instead of a stateless HMAC cookie (so
logout is real), and the lockout check happens first. Deliberately NOT lifted: any
first-run "create the admin account" flow -- on a public app that's a land-grab, so
missing ADMIN_USER/ADMIN_PASSWORD_HASH means the app refuses to start (see app.py).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import secrets

import db

COOKIE_NAME = "__Host-dt_session"  # pragma: allowlist secret
SESSION_HOURS = 12
PBKDF2_ITERATIONS = 600_000

LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_MAX_FAILURES = 8


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _now_iso() -> str:
    return _now().isoformat()


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """iterations$salt_hex$digest_hex. The iteration count travels with the hash so
    bumping PBKDF2_ITERATIONS later never breaks previously-stored hashes."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        iterations_s, salt_hex, digest_hex = stored_hash.split("$", 2)
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest.hex(), digest_hex)


def admin_configured() -> bool:
    return bool(os.environ.get("ADMIN_USER")) and bool(os.environ.get("ADMIN_PASSWORD_HASH"))


def is_locked_out(username: str, ip: str) -> bool:
    """Check BEFORE hashing. PBKDF2 at 600k iterations is ~200ms; without this check
    the login route is an unauthenticated CPU amplifier -- a couple req/s pins the
    whole app across both gunicorn workers."""
    cutoff = (_now() - datetime.timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM login_attempts
               WHERE (username = ? OR ip = ?) AND success = 0 AND created_at > ?""",
            (username, ip, cutoff),
        ).fetchone()
    return row["n"] >= LOCKOUT_MAX_FAILURES


def record_login_attempt(username: str, ip: str, success: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO login_attempts (username, ip, success, created_at) VALUES (?, ?, ?, ?)",
            (username, ip, int(success), _now_iso()),
        )


def check_login(username: str, password: str) -> bool:
    """Fails closed if ADMIN_USER/ADMIN_PASSWORD_HASH are unset. Always runs
    verify_password even on a username mismatch so a wrong username doesn't
    short-circuit before the deliberately-slow hash comparison."""
    want_user = os.environ.get("ADMIN_USER", "")
    want_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    if not want_user or not want_hash:
        return False
    user_ok = hmac.compare_digest(username.encode(), want_user.encode())
    pass_ok = verify_password(password, want_hash)
    return user_ok and pass_ok


def create_session(username: str) -> tuple[str, str]:
    """Returns (session_id, csrf_token). session_id is the cookie value; csrf_token
    is bound to the session row, not derivable from the cookie alone."""
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)  # pragma: allowlist secret
    expires = (_now() + datetime.timedelta(hours=SESSION_HOURS)).isoformat()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO sessions (id, username, csrf_token, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, username, csrf_token, _now_iso(), expires),
        )
    return session_id, csrf_token


def get_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    with db.connect() as conn:
        row = conn.execute(
            """SELECT id, username, csrf_token, expires_at, revoked_at FROM sessions
               WHERE id = ?""",
            (session_id,),
        ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= _now_iso():
        return None
    return dict(row)


def revoke_session(session_id: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ?", (_now_iso(), session_id)
        )


def verify_csrf(session: dict, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, session["csrf_token"])
