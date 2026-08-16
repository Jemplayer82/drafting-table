from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "drafting.db"

# Other modules must reference db.MEDIA_DIR via attribute lookup, not from-import,
# because tests reload the db module and only live attributes see the new value.
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "./media"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    source_url    TEXT,
    title         TEXT,
    tag           TEXT,
    note_md       TEXT,
    alt_text      TEXT,
    raw_text      TEXT,
    media_id      TEXT,
    thumb_media_id TEXT,
    thumb_w       INTEGER,
    thumb_h       INTEGER,
    content_hash  TEXT,
    position      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    job_id        INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_project ON items (project_id, position, id);

CREATE TABLE IF NOT EXISTS swatches (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    hex      TEXT NOT NULL,
    label    TEXT,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_swatches_item ON swatches (item_id, position);

CREATE TABLE IF NOT EXISTS syntheses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    direction_md    TEXT NOT NULL,
    questions_json  TEXT NOT NULL,
    model           TEXT,
    item_count      INTEGER,
    trigger_item_id INTEGER,
    job_id          INTEGER,
    created_at      TEXT NOT NULL,
    UNIQUE (project_id, version)
);
CREATE INDEX IF NOT EXISTS idx_syntheses_lookup ON syntheses (project_id, version DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    body_md       TEXT NOT NULL,
    rationale_md  TEXT,
    source        TEXT NOT NULL,
    status        TEXT NOT NULL,
    superseded_by INTEGER REFERENCES decisions(id),
    job_id        INTEGER,
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions (project_id, status, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    item_id     INTEGER,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    phase       TEXT,
    worker_id   TEXT,
    attempt     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    heartbeat_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_live    ON jobs (status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_queued  ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS media (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    mime        TEXT NOT NULL,
    width       INTEGER,
    height      INTEGER,
    byte_size   INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    csrf_token  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    ip          TEXT NOT NULL,
    success     INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_lockout ON login_attempts (username, created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip       ON login_attempts (ip, created_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_bucket ON rate_limits (bucket, created_at);
"""

# House rule (see TradingAgents/web/db.py): a new column goes in BOTH SCHEMA
# above (for fresh installs) AND here (for existing databases).
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = []


def _run_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _run_column_migrations(conn)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


# ---- Column allowlists (guarded interpolation — never build UPDATE SQL from
# unfiltered request data) ----

ITEM_WRITABLE = {
    "title", "tag", "note_md", "alt_text", "status", "error", "position",
    "source_url", "raw_text", "media_id", "thumb_media_id", "thumb_w", "thumb_h",
    "content_hash", "job_id",
}
ITEM_USER_EDITABLE = {"title", "tag", "note_md", "position"}


def update_item(item_id: int, **fields: object) -> None:
    bad = set(fields) - ITEM_WRITABLE
    if bad:
        raise ValueError(f"not writable: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE items SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), item_id),
        )


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).isoformat()