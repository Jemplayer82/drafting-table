from __future__ import annotations

import os
import re
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


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def create_project(name: str, note: str | None) -> tuple[int, str]:
    """Inserts a project, generating a unique slug from name. On a UNIQUE-constraint
    collision on projects.slug, retries the INSERT with -2, -3, ... suffixes -- a
    retry-against-the-DB loop, not check-then-insert, to avoid a race between two
    concurrent creates with the same name. Returns (new project id, final slug)."""
    base = _slugify(name) or "project"
    now = _now()
    candidate = base
    suffix = 1
    with connect() as conn:
        while True:
            try:
                cur = conn.execute(
                    "INSERT INTO projects (name, slug, note, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, candidate, note, now, now),
                )
                return cur.lastrowid, candidate
            except sqlite3.IntegrityError:
                suffix += 1
                candidate = f"{base}-{suffix}"


def move_item(item_id: int, direction: str) -> bool:
    """Moves this item one step in its project's render order ('up' means toward
    the front, 'down' toward the end), matching project_detail()'s own
    'ORDER BY position, id' order. When the item and its neighbor have distinct
    positions, their position values are swapped. When they are tied on position,
    the full project's items are renumbered to sequential distinct positions so
    the two rows actually exchange order. Runs in one transaction. Returns True
    if a move happened, False if item_id doesn't exist or has no neighbor in that
    direction (already first/last). Raises ValueError if direction isn't
    'up'/'down'."""
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT project_id, position FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            if direction == "up":
                neighbor = conn.execute(
                    "SELECT id, position FROM items WHERE project_id = ? AND "
                    "(position < ? OR (position = ? AND id < ?)) "
                    "ORDER BY position DESC, id DESC LIMIT 1",
                    (row["project_id"], row["position"], row["position"], item_id),
                ).fetchone()
            else:
                neighbor = conn.execute(
                    "SELECT id, position FROM items WHERE project_id = ? AND "
                    "(position > ? OR (position = ? AND id > ?)) "
                    "ORDER BY position ASC, id ASC LIMIT 1",
                    (row["project_id"], row["position"], row["position"], item_id),
                ).fetchone()
            if neighbor is None:
                conn.execute("ROLLBACK")
                return False
            now = _now()
            if neighbor["position"] != row["position"]:
                conn.execute(
                    "UPDATE items SET position = ?, updated_at = ? WHERE id = ?",
                    (neighbor["position"], now, item_id),
                )
                conn.execute(
                    "UPDATE items SET position = ?, updated_at = ? WHERE id = ?",
                    (row["position"], now, neighbor["id"]),
                )
            else:
                # Tie: a plain position swap would be a no-op. Renumber the
                # whole project in render order, swapping the moving item with
                # its immediate neighbor in that order, so their relative order
                # really flips.
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM items WHERE project_id = ? ORDER BY position, id",
                        (row["project_id"],),
                    )
                ]
                idx = ids.index(item_id)
                swap_idx = idx - 1 if direction == "up" else idx + 1
                ids[idx], ids[swap_idx] = ids[swap_idx], ids[idx]
                for pos, id_ in enumerate(ids):
                    conn.execute(
                        "UPDATE items SET position = ?, updated_at = ? WHERE id = ?",
                        (pos, now, id_),
                    )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


def delete_item(item_id: int) -> list[str]:
    """Deletes the item row (swatches cascade automatically via the existing ON
    DELETE CASCADE FK, since connect() sets PRAGMA foreign_keys=ON) and its media
    rows (media_id / thumb_media_id are TEXT ids into the media table, NOT declared
    as FKs in the schema, so they must be deleted explicitly here). Returns the
    media.path values that existed, for the caller to unlink from disk under
    db.MEDIA_DIR -- this function only ever touches the DB, never the filesystem.
    Returns [] if item_id doesn't exist. Runs in one transaction."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT media_id, thumb_media_id FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return []
            media_ids = [m for m in (row["media_id"], row["thumb_media_id"]) if m]
            paths = []
            for mid in media_ids:
                mrow = conn.execute("SELECT path FROM media WHERE id = ?", (mid,)).fetchone()
                if mrow:
                    paths.append(mrow["path"])
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            if media_ids:
                placeholders = ",".join("?" * len(media_ids))
                conn.execute(f"DELETE FROM media WHERE id IN ({placeholders})", media_ids)
            conn.execute("COMMIT")
            return paths
        except Exception:
            conn.execute("ROLLBACK")
            raise


def replace_swatches(item_id: int, swatches: list[dict]) -> None:
    """Replaces ALL swatches for item_id: DELETE existing rows then INSERT the given
    ones, in one transaction (swatches are agent-derived/re-derivable, unlike
    decisions -- full replace is intentional and safe, per the plan doc). Each dict
    needs 'hex' (str) and optionally 'label' (str or None); position is assigned by
    list order (0-indexed). Caller must validate hex format before calling -- this
    function does not."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM swatches WHERE item_id = ?", (item_id,))
            for i, sw in enumerate(swatches):
                conn.execute(
                    "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
                    (item_id, sw["hex"], sw.get("label"), i),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def insert_decision(project_id: int, body_md: str, rationale_md: str | None) -> int:
    """Inserts a new decisions row: source='user', status='accepted', decided_at=now.
    superseded_by and job_id are left NULL (both nullable, no DEFAULT needed).
    Single-statement write, atomic under connect()'s autocommit mode -- no explicit
    transaction needed. Returns the new row's id."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
            "created_at, decided_at) VALUES (?, ?, ?, 'user', 'accepted', ?, ?)",
            (project_id, body_md, rationale_md, now, now),
        )
        return cur.lastrowid


def supersede_decision(
    decision_id: int, project_id: int, body_md: str, rationale_md: str | None
) -> int | None:
    """Append-only edit: INSERTs a new decisions row (source='user', status='accepted')
    and UPDATEs the OLD row to set superseded_by to the new row's id -- NEVER UPDATEs
    body_md/rationale_md/status/created_at/decided_at on the old row. Both statements
    run in one transaction. project_id scopes the lookup as a defense-in-depth check
    (the old row must belong to that project), and the old row must still be the
    current/live decision in its chain (status='accepted' and superseded_by IS NULL).
    Returns the new row's id, or None (no changes made) if decision_id doesn't exist,
    doesn't belong to project_id, or has already been superseded."""
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            old = conn.execute(
                "SELECT id FROM decisions WHERE id = ? AND project_id = ? "
                "AND status = 'accepted' AND superseded_by IS NULL",
                (decision_id, project_id),
            ).fetchone()
            if old is None:
                conn.execute("ROLLBACK")
                return None
            cur = conn.execute(
                "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
                "created_at, decided_at) VALUES (?, ?, ?, 'user', 'accepted', ?, ?)",
                (project_id, body_md, rationale_md, now, now),
            )
            new_id = cur.lastrowid
            conn.execute(
                "UPDATE decisions SET superseded_by = ? WHERE id = ?", (new_id, decision_id)
            )
            conn.execute("COMMIT")
            return new_id
        except Exception:
            conn.execute("ROLLBACK")
            raise