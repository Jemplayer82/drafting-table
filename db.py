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
    trigger_item_id INTEGER,
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
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("jobs", "trigger_item_id", "INTEGER"),
]


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
    # busy_timeout MUST be set before journal_mode=WAL, not after: a brand-new
    # connection's busy_timeout defaults to 0 (fail immediately on contention),
    # and enabling WAL mode itself needs a brief exclusive lock on the database
    # header. Setting busy_timeout second left exactly that PRAGMA call with no
    # retry tolerance -- multiple processes racing to initialize a fresh,
    # not-yet-existing database file (e.g. gunicorn's multiple workers, or a
    # separate worker container, all first-booting against an empty volume)
    # could and did hit `sqlite3.OperationalError: database is locked` right
    # here, with gunicorn's arbiter escalating that into a full boot failure.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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


def insert_media(
    media_id: str, path: str, mime: str, width: int, height: int, byte_size: int
) -> None:
    """Inserts a media row for a file that has already been written to disk at
    db.MEDIA_DIR / path. Single-statement write under this file's normal
    autocommit connect() -- no explicit BEGIN IMMEDIATE needed, matching the shape
    of this file's other single-statement inserts. Caller has already generated
    media_id via secrets.token_hex(16) and written the bytes to disk."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO media (id, path, mime, width, height, byte_size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (media_id, path, mime, width, height, byte_size, _now()),
        )


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


# ---------------------------------------------------------------------------
# Job pipeline SQL layer
# ---------------------------------------------------------------------------

def claim_next_job(worker_id: str) -> sqlite3.Row | None:
    """Atomically claims the oldest queued job. SQLite serializes writers, so at most
    one caller's UPDATE can satisfy the outer AND status='queued' guard; losers get
    zero RETURNING rows and fetchone() returns None."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE jobs SET status='running', worker_id=?, started_at=?, heartbeat_at=?
            WHERE id = (
                SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1
            )
              AND status='queued'
            RETURNING *
            """,
            (worker_id, now, now),
        )
        row = cur.fetchone()
        assert row is None or cur.rowcount == 1
        return row


def set_job_phase(job_id: int, phase: str) -> None:
    """Records a job phase transition; a phase transition also counts as a heartbeat."""
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET phase=?, heartbeat_at=? WHERE id=?",
            (phase, _now(), job_id),
        )


def complete_ingest_job(
    job_id: int,
    item_id: int,
    title: str,
    *,
    tag: str | None = None,
    note_md: str | None = None,
    alt_text: str | None = None,
    media_id: str | None = None,
    thumb_media_id: str | None = None,
    thumb_w: int | None = None,
    thumb_h: int | None = None,
) -> None:
    """Marks an ingest item as ready with its extracted title and optional
    analyze_item()-derived tag, note_md, and alt_text, plus (for url items with a
    fetched og:image) its media fields. tag, note_md, alt_text, and all four
    media fields default to None -- the existing 3-positional-arg call shape is
    unaffected and continues to leave those columns NULL exactly as before.
    Keeping title+tag+note_md+alt_text+media fields in one atomic UPDATE avoids
    a second update_item round-trip. Both updates share one timestamp and run in
    one transaction."""
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE items SET status='ready', title=?, tag=?, note_md=?, alt_text=?, "
                "media_id=?, thumb_media_id=?, thumb_w=?, thumb_h=?, updated_at=? WHERE id=?",
                (
                    title, tag, note_md, alt_text, media_id,
                    thumb_media_id, thumb_w, thumb_h, now, item_id,
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=?, heartbeat_at=? WHERE id=?",
                (now, now, job_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def complete_job(job_id: int) -> None:
    """Marks a job as done without touching items or syntheses (used by resynthesize)."""
    with connect() as conn:
        now = _now()
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=?, heartbeat_at=? WHERE id=?",
            (now, now, job_id),
        )


def chain_resynthesize_job(
    project_id: int, trigger_item_id: int | None = None
) -> None:
    """Debounced insert of a queued 'resynthesize' job. If a queued or running
    resynthesize job already exists for the project, this is a no-op -- and in
    that case the ALREADY-QUEUED job's trigger_item_id is NOT overwritten, it
    keeps whichever item first triggered it. trigger_item_id defaults to None so
    every existing single-positional-arg call site keeps working unchanged."""
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (project_id, kind, status, trigger_item_id, created_at)
            SELECT ?, 'resynthesize', 'queued', ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE project_id = ? AND kind = 'resynthesize'
                  AND status IN ('queued', 'running')
            )
            """,
            (project_id, trigger_item_id, now, project_id),
        )


def fail_job(job_id: int, item_id: int | None, error: str) -> None:
    """Marks a job as failed with the given error message. If item_id is provided,
    the matching item is also marked failed. Resynthesize jobs pass item_id=None."""
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, now, job_id),
            )
            if item_id is not None:
                conn.execute(
                    "UPDATE items SET status='failed', error=?, updated_at=? WHERE id=?",
                    (error, now, item_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def find_stale_running_jobs(cutoff: str) -> list[sqlite3.Row]:
    """Returns running jobs whose heartbeat is older than the given ISO cutoff."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, item_id, project_id, kind
            FROM jobs
            WHERE status='running' AND heartbeat_at < ?
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()


def find_all_running_jobs() -> list[sqlite3.Row]:
    """Returns every currently running job, regardless of heartbeat age."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, item_id, project_id, kind
            FROM jobs
            WHERE status='running'
            ORDER BY id
            """
        ).fetchall()


def get_item(item_id: int) -> sqlite3.Row | None:
    """Returns the full items row for item_id, or None if it doesn't exist."""
    with connect() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def get_media(media_id: str) -> sqlite3.Row | None:
    """Returns the full media row for media_id, or None if it doesn't exist."""
    with connect() as conn:
        return conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()


def _create_item_and_ingest_job(
    project_id: int, kind: str, **item_fields: object
) -> tuple[int, int]:
    """Shared by create_note_and_ingest_job/create_url_and_ingest_job: inserts a new
    item of the given kind in pending status plus a queued ingest job for it, all in
    one transaction. item_fields values are inserted as literal columns matching
    their key names -- every key must already be in ITEM_WRITABLE (mirrors
    update_item's own guard; both current call sites below pass a single fixed,
    hardcoded field name, so this can never be reached with an untrusted column
    name, but the check costs nothing and matches this file's defense-in-depth
    convention). The item is appended at the next position in the project. Returns
    (item_id, job_id)."""
    bad = set(item_fields) - ITEM_WRITABLE
    if bad:
        raise ValueError(f"not writable: {bad}")
    now = _now()
    columns = ", ".join(item_fields.keys())
    placeholders = ", ".join("?" for _ in item_fields)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos FROM items WHERE project_id=?",
                (project_id,),
            ).fetchone()
            position = pos_row["next_pos"]
            insert_sql = (
                f"INSERT INTO items "
                f"(project_id, kind, status, {columns}, position, created_at, updated_at) "
                f"VALUES (?, ?, 'pending', {placeholders}, ?, ?, ?)"
            )
            cur = conn.execute(
                insert_sql, (project_id, kind, *item_fields.values(), position, now, now)
            )
            item_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO jobs (project_id, item_id, kind, status, created_at) "
                "VALUES (?, ?, 'ingest', 'queued', ?)",
                (project_id, item_id, now),
            )
            job_id = cur.lastrowid
            conn.execute("COMMIT")
            return item_id, job_id
        except Exception:
            conn.execute("ROLLBACK")
            raise


def create_note_and_ingest_job(project_id: int, raw_text: str) -> tuple[int, int]:
    return _create_item_and_ingest_job(project_id, "note", raw_text=raw_text)


def create_url_and_ingest_job(project_id: int, source_url: str) -> tuple[int, int]:
    return _create_item_and_ingest_job(project_id, "url", source_url=source_url)


def create_image_and_ingest_job(project_id: int, media_id: str) -> tuple[int, int]:
    return _create_item_and_ingest_job(project_id, "image", media_id=media_id)


def list_active_jobs(project_id: int) -> list[sqlite3.Row]:
    """Returns queued or running jobs for a project, oldest first."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, kind, status, phase
            FROM jobs
            WHERE project_id=? AND status IN ('queued', 'running')
            ORDER BY created_at
            """,
            (project_id,),
        ).fetchall()


def items_rev(project_id: int) -> str:
    """Returns a composite revision token for a project's item set. The same DB state
    always yields the same token, and any add/remove/update of an item changes it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MAX(updated_at) AS m "
            "FROM items WHERE project_id=?",
            (project_id,),
        ).fetchone()
    return f"{row['c']}:{row['m'] or ''}"


def latest_synthesis_version(project_id: int) -> int:
    """Returns the highest synthesis version for a project, or 0 if none exist."""
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM syntheses WHERE project_id=?",
            (project_id,),
        ).fetchone()
    return row["v"] or 0


def live_jobs_by_item(
    project_id: int, conn: sqlite3.Connection | None = None
) -> dict[int, sqlite3.Row]:
    """Maps each project item that has a queued/running job to its live job row.
    Jobs with item_id=NULL (e.g. resynthesize jobs) are excluded. If conn is given,
    the query runs on that connection instead of opening a new one -- the caller owns
    that connection's lifecycle/transaction, which lets this be read alongside other
    project state (e.g. _render_project's items query) from one consistent snapshot
    instead of a separate autocommit connection. Defaults to opening its own connection
    (matching the previous behavior) when conn is None."""

    def _fetch(c: sqlite3.Connection) -> dict[int, sqlite3.Row]:
        rows = c.execute(
            """
            SELECT id, item_id, status, phase
            FROM jobs
            WHERE project_id=? AND item_id IS NOT NULL
              AND status IN ('queued', 'running')
            """,
            (project_id,),
        ).fetchall()
        return {row["item_id"]: row for row in rows}

    if conn is not None:
        return _fetch(conn)
    with connect() as conn:
        return _fetch(conn)


# ---------------------------------------------------------------------------
# Resynthesis context / writes
# ---------------------------------------------------------------------------


def get_resynthesis_context(project_id: int) -> dict:
    """Gathers everything agent.resynthesize_project() needs in one read: project
    name, up to 25 most-recently-updated status='ready' items (oldest-of-the-25
    first, so the digest built from this list reads chronologically), each item's
    swatches, every currently-live accepted decision (status='accepted' AND
    superseded_by IS NULL), and the current synthesis row if one exists (highest
    version). NEVER reads media_id/thumb_media_id/thumb_w/thumb_h/raw_text/
    alt_text or any other blob-adjacent column -- only id/title/tag/note_md/
    source_url/kind per item, and 'id' is stripped from the returned item dicts
    (used internally only, to batch-attach swatches). Does NOT enforce the
    '>=3 ready items' product threshold -- callers (worker.py) check
    context['item_count'] themselves before deciding whether to call the agent
    at all; this function must not crash for a project with 0 ready items. Runs
    on one connection under a plain BEGIN (not BEGIN IMMEDIATE -- read-only, no
    need to contend for a write lock) so all reads reflect one consistent
    snapshot, matching app.py's _render_project's own reasoning for doing the
    same thing."""
    with connect() as conn:
        conn.execute("BEGIN")
        try:
            proj = conn.execute(
                "SELECT name FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            item_rows = conn.execute(
                "SELECT id, title, tag, note_md, source_url, kind FROM items "
                "WHERE project_id = ? AND status = 'ready' "
                "ORDER BY updated_at DESC LIMIT 25",
                (project_id,),
            ).fetchall()
            item_rows = list(reversed(item_rows))  # oldest-of-the-25 first
            swatches_by_item: dict[int, list[dict]] = {}
            if item_rows:
                ids = [r["id"] for r in item_rows]
                placeholders = ",".join("?" * len(ids))
                swatch_rows = conn.execute(
                    f"SELECT item_id, hex, label FROM swatches "
                    f"WHERE item_id IN ({placeholders}) ORDER BY item_id, position",
                    ids,
                ).fetchall()
                for row in swatch_rows:
                    swatches_by_item.setdefault(row["item_id"], []).append(
                        {"hex": row["hex"], "label": row["label"]}
                    )
            decision_rows = conn.execute(
                "SELECT body_md, rationale_md FROM decisions "
                "WHERE project_id = ? AND status = 'accepted' AND superseded_by IS NULL "
                "ORDER BY created_at",
                (project_id,),
            ).fetchall()
            synth_row = conn.execute(
                "SELECT version, direction_md, questions_json FROM syntheses "
                "WHERE project_id = ? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    items = [
        {
            "title": r["title"],
            "tag": r["tag"],
            "note_md": r["note_md"],
            "source_url": r["source_url"],
            "kind": r["kind"],
            "swatches": swatches_by_item.get(r["id"], []),
        }
        for r in item_rows
    ]
    return {
        "project_name": proj["name"] if proj else "",
        "items": items,
        "item_count": len(items),
        "accepted_decisions": [
            {"body_md": r["body_md"], "rationale_md": r["rationale_md"]}
            for r in decision_rows
        ],
        "previous_synthesis": (
            {
                "version": synth_row["version"],
                "direction_md": synth_row["direction_md"],
                "questions_json": synth_row["questions_json"],
            }
            if synth_row
            else None
        ),
    }


def propose_decision(
    project_id: int, body_md: str, rationale_md: str | None, job_id: int
) -> int:
    """Inserts a new decisions row: source='agent', status='proposed',
    superseded_by=NULL, decided_at=NULL (both omitted from the INSERT column
    list, so they default to NULL -- neither column has a DEFAULT clause; NULL
    is simply what an omitted nullable column gets). Single-statement write,
    atomic under connect()'s autocommit mode. THIS IS THE ONLY WRITE PATH the
    agent-driven resynthesis code may ever use to touch the decisions table --
    it must NEVER UPDATE or DELETE any existing decisions row; that absence of
    any UPDATE/DELETE is the entire safety property the append-only decisions
    guarantee depends on. Do not wrap this call in any try/except that could
    swallow a failure. Returns the new row's id."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
            "job_id, created_at) VALUES (?, ?, ?, 'agent', 'proposed', ?, ?)",
            (project_id, body_md, rationale_md, job_id, now),
        )
        return cur.lastrowid


def insert_synthesis(
    project_id: int,
    direction_md: str,
    questions_json: str,
    item_count: int,
    trigger_item_id: int | None,
    job_id: int,
) -> int:
    """INSERTs one new syntheses row at version = COALESCE(MAX(version),0)+1 for
    this project, computed as part of the SAME atomic SQL statement as the
    INSERT itself (INSERT ... SELECT ... is one write statement; under
    connect()'s autocommit/isolation_level=None mode SQLite still wraps a
    single statement in its own implicit transaction and serializes writers at
    the file level -- the same single-statement atomicity claim_next_job's
    UPDATE...WHERE and chain_resynthesize_job's INSERT...WHERE NOT EXISTS
    already rely on elsewhere in this file -- so this is race-safe against a
    concurrent second resynthesize job for the same project with no explicit
    BEGIN IMMEDIATE needed). An aggregate query with no GROUP BY always returns
    exactly one row even when zero rows match project_id, so this also
    correctly produces version=1 on a project's first-ever synthesis with no
    special-casing. NEVER UPDATEs or DELETEs an existing syntheses row --
    versioned history is append-only by construction, mirroring the decisions
    guarantee. Does NOT catch sqlite3.IntegrityError from the
    UNIQUE(project_id, version) constraint -- if it somehow fires, the job must
    fail loudly, never silently. model is hardcoded to 'claude-sonnet-5',
    matching run_claude's own default. Returns the new row's id."""
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "model, item_count, trigger_item_id, job_id, created_at) "
            "SELECT ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ? "
            "FROM syntheses WHERE project_id = ?",
            (
                project_id,
                direction_md,
                questions_json,
                "claude-sonnet-5",
                item_count,
                trigger_item_id,
                job_id,
                now,
                project_id,
            ),
        )
        return cur.lastrowid
