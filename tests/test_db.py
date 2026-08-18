from __future__ import annotations

import importlib
import threading


def test_init_db_is_idempotent(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    db.init_db()  # must not raise on a second boot

    with db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    for expected in (
        "projects", "items", "swatches", "syntheses", "decisions",
        "jobs", "media", "sessions", "login_attempts", "rate_limits",
    ):
        assert expected in tables


def test_init_db_creates_media_dir(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    assert db.MEDIA_DIR.is_dir()


def test_update_item_rejects_non_allowlisted_columns(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    try:
        db.update_item(1, source_url_injected="'; DROP TABLE items; --")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-allowlisted column")


def test_create_project_generates_slug_from_name(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, slug = db.create_project("My Cool Project!", None)
    assert slug == "my-cool-project"
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["name"] == "My Cool Project!"


def test_create_project_slug_collision_appends_suffix(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid1, slug1 = db.create_project("Same Name", None)
    pid2, slug2 = db.create_project("Same Name", None)
    assert slug1 == "same-name"
    assert slug2 == "same-name-2"
    with db.connect() as conn:
        rows = conn.execute("SELECT id, slug FROM projects ORDER BY id").fetchall()
    assert rows[0]["id"] == pid1
    assert rows[0]["slug"] == slug1
    assert rows[1]["id"] == pid2
    assert rows[1]["slug"] == slug2


def test_create_project_falls_back_to_project_slug_for_unslugifiable_name(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, slug = db.create_project("!!!", None)
    assert slug == "project"
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["name"] == "!!!"


def _insert_item(conn, project_id, db, position=0, media_id=None, thumb_media_id=None):
    now = db._now()
    cur = conn.execute(
        "INSERT INTO items (project_id, kind, status, position, media_id, thumb_media_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, "test", "todo", position, media_id, thumb_media_id, now, now),
    )
    return cur.lastrowid


def _insert_job(
    conn,
    project_id,
    db,
    kind="ingest",
    status="queued",
    created_at=None,
    item_id=None,
):
    if created_at is None:
        created_at = db._now()
    cur = conn.execute(
        "INSERT INTO jobs (project_id, kind, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        (project_id, kind, status, created_at),
    )
    job_id = cur.lastrowid
    if item_id is not None:
        conn.execute(
            "UPDATE jobs SET item_id = ? WHERE id = ?",
            (item_id, job_id),
        )
    return job_id


def _insert_ready_item(
    conn,
    project_id,
    db,
    *,
    title=None,
    tag=None,
    note_md=None,
    source_url=None,
    kind="note",
    updated_at=None,
):
    updated_at = updated_at or db._now()
    cur = conn.execute(
        "INSERT INTO items (project_id, kind, status, title, tag, note_md, source_url, "
        "position, created_at, updated_at) "
        "VALUES (?, ?, 'ready', ?, ?, ?, ?, 0, ?, ?)",
        (project_id, kind, title, tag, note_md, source_url, updated_at, updated_at),
    )
    return cur.lastrowid


def test_move_item_swaps_positions(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Move Test", None)
    with db.connect() as conn:
        i1 = _insert_item(conn, pid, db, position=0)
        i2 = _insert_item(conn, pid, db, position=1)

    assert db.move_item(i1, "down") is True

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, position FROM items WHERE project_id = ? ORDER BY id", (pid,)
        ).fetchall()
    assert rows[0]["id"] == i1
    assert rows[0]["position"] == 1
    assert rows[1]["id"] == i2
    assert rows[1]["position"] == 0


def test_move_item_at_top_returns_false(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Top Test", None)
    with db.connect() as conn:
        i1 = _insert_item(conn, pid, db, position=0)

    assert db.move_item(i1, "up") is False

    with db.connect() as conn:
        pos = conn.execute("SELECT position FROM items WHERE id = ?", (i1,)).fetchone()["position"]
    assert pos == 0


def test_move_item_at_bottom_returns_false(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Bottom Test", None)
    with db.connect() as conn:
        i1 = _insert_item(conn, pid, db, position=0)

    assert db.move_item(i1, "down") is False

    with db.connect() as conn:
        pos = conn.execute("SELECT position FROM items WHERE id = ?", (i1,)).fetchone()["position"]
    assert pos == 0


def test_move_item_missing_item_returns_false(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    assert db.move_item(999999, "up") is False


def test_move_item_invalid_direction_raises_value_error(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Direction Test", None)
    with db.connect() as conn:
        i1 = _insert_item(conn, pid, db, position=0)
    try:
        db.move_item(i1, "sideways")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for invalid direction")


def test_move_item_tied_position_swaps_by_id(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Tie Test", None)
    with db.connect() as conn:
        older = _insert_item(conn, pid, db, position=5)
        newer = _insert_item(conn, pid, db, position=5)

    # Moving the higher-id item up must find the lower-id neighbor.
    assert db.move_item(newer, "up") is True

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, position FROM items WHERE project_id = ? ORDER BY position, id",
            (pid,),
        ).fetchall()
    assert rows[0]["id"] == newer
    assert rows[0]["position"] == 0
    assert rows[1]["id"] == older
    assert rows[1]["position"] == 1

    # Reset both items to the same position.
    db.update_item(older, position=5)
    db.update_item(newer, position=5)

    # Moving the lower-id item down must find the higher-id neighbor.
    assert db.move_item(older, "down") is True

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, position FROM items WHERE project_id = ? ORDER BY position, id",
            (pid,),
        ).fetchall()
    assert rows[0]["id"] == newer
    assert rows[0]["position"] == 0
    assert rows[1]["id"] == older
    assert rows[1]["position"] == 1


def test_move_item_tied_position_picks_immediate_neighbor_by_id(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Three-way Tie Test", None)
    with db.connect() as conn:
        a = _insert_item(conn, pid, db, position=10)
        b = _insert_item(conn, pid, db, position=10)
        c = _insert_item(conn, pid, db, position=10)

    # Moving the middle item up must swap with its immediate lower-id neighbor (a).
    assert db.move_item(b, "up") is True

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE project_id = ? ORDER BY position, id",
            (pid,),
        ).fetchall()
    assert [row["id"] for row in rows] == [b, a, c]

    # Reset positions so all three are tied again.
    db.update_item(a, position=10)
    db.update_item(b, position=10)
    db.update_item(c, position=10)

    # Moving the middle item down must swap with its immediate higher-id neighbor (c).
    assert db.move_item(b, "down") is True

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE project_id = ? ORDER BY position, id",
            (pid,),
        ).fetchall()
    assert [row["id"] for row in rows] == [a, c, b]


def test_delete_item_removes_row_and_returns_media_paths(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Delete Test", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        media_id = f"m{item_id}"
        thumb_id = f"t{item_id}"
        conn.execute(
            "UPDATE items SET media_id = ?, thumb_media_id = ? WHERE id = ?",
            (media_id, thumb_id, item_id),
        )
        conn.execute(
            "INSERT INTO media (id, path, mime, created_at) VALUES (?, ?, ?, ?)",
            (media_id, "full.jpg", "image/jpeg", db._now()),
        )
        conn.execute(
            "INSERT INTO media (id, path, mime, created_at) VALUES (?, ?, ?, ?)",
            (thumb_id, "thumb.jpg", "image/jpeg", db._now()),
        )
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#ff0000", "red", 0),
        )

    paths = db.delete_item(item_id)
    assert sorted(paths) == sorted(["full.jpg", "thumb.jpg"])

    with db.connect() as conn:
        item_row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        media_rows = conn.execute(
            "SELECT id FROM media WHERE id IN (?, ?)", (media_id, thumb_id)
        ).fetchall()
        swatch_rows = conn.execute(
            "SELECT id FROM swatches WHERE item_id = ?", (item_id,)
        ).fetchall()
    assert item_row is None
    assert len(media_rows) == 0
    assert len(swatch_rows) == 0


def test_delete_item_missing_returns_empty_list(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    assert db.delete_item(999999) == []


def test_replace_swatches_replaces_all(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Swatch Test", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)

    db.replace_swatches(item_id, [{"hex": "#111111", "label": "a"}])
    db.replace_swatches(item_id, [{"hex": "#222222", "label": "b"}])

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["hex"] == "#222222"
    assert rows[0]["label"] == "b"


def test_replace_swatches_clears_all_with_empty_list(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Swatch Clear Test", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#ff0000", "red", 0),
        )
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#00ff00", "green", 1),
        )
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#0000ff", "blue", 2),
        )

    db.replace_swatches(item_id, [])

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    assert len(rows) == 0


def test_replace_swatches_shrinks_to_fewer_swatches(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Swatch Shrink Test", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#ff0000", "red", 0),
        )
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#00ff00", "green", 1),
        )
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, "#0000ff", "blue", 2),
        )

    db.replace_swatches(
        item_id,
        [
            {"hex": "#111111", "label": "one"},
            {"hex": "#222222", "label": "two"},
        ],
    )

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["hex"] == "#111111"
    assert rows[0]["label"] == "one"
    assert rows[1]["hex"] == "#222222"
    assert rows[1]["label"] == "two"


def test_insert_decision_creates_accepted_user_row(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Decision Insert", None)
    did = db.insert_decision(pid, "Use blue", None)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert row["project_id"] == pid
    assert row["body_md"] == "Use blue"
    assert row["rationale_md"] is None
    assert row["source"] == "user"
    assert row["status"] == "accepted"
    assert row["superseded_by"] is None
    assert row["decided_at"] is not None


def test_supersede_decision_does_not_mutate_old_row(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Decision Supersede", None)
    old_id = db.insert_decision(pid, "Use blue", "**Type:** because")
    new_id = db.supersede_decision(old_id, pid, "Use green", "**Type:** changed")

    assert new_id is not None

    with db.connect() as conn:
        old = conn.execute("SELECT * FROM decisions WHERE id = ?", (old_id,)).fetchone()
        new = conn.execute("SELECT * FROM decisions WHERE id = ?", (new_id,)).fetchone()
    assert old["body_md"] == "Use blue"
    assert old["rationale_md"] == "**Type:** because"
    assert old["superseded_by"] == new_id
    assert new["body_md"] == "Use green"
    assert new["rationale_md"] == "**Type:** changed"
    assert new["status"] == "accepted"
    assert new["source"] == "user"
    assert new["superseded_by"] is None


def test_supersede_decision_wrong_project_returns_none(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid_a, _ = db.create_project("Project A", None)
    pid_b, _ = db.create_project("Project B", None)
    decision_id = db.insert_decision(pid_a, "Use blue", None)

    result = db.supersede_decision(decision_id, pid_b, "Use green", None)
    assert result is None

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    assert row["project_id"] == pid_a
    assert row["body_md"] == "Use blue"
    assert row["superseded_by"] is None


def test_supersede_decision_already_superseded_returns_none(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Double Supersede", None)
    original_id = db.insert_decision(pid, "Use blue", "**Type:** because")
    first_new_id = db.supersede_decision(original_id, pid, "Use green", "**Type:** changed")

    assert first_new_id is not None

    # Simulate a double-click / back-button resubmit: try to supersede the same
    # already-superseded original decision again.
    second_new_id = db.supersede_decision(original_id, pid, "Use red", "**Type:** changed again")
    assert second_new_id is None

    with db.connect() as conn:
        original = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (original_id,)
        ).fetchone()
        first_new = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (first_new_id,)
        ).fetchone()
        decision_count = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE project_id = ?", (pid,)
        ).fetchone()["n"]

    # The original row still points to the first superseding row.
    assert original["superseded_by"] == first_new_id

    # The first superseding row is unchanged and remains the unique current row.
    assert first_new["superseded_by"] is None
    assert first_new["body_md"] == "Use green"
    assert first_new["rationale_md"] == "**Type:** changed"
    assert first_new["status"] == "accepted"

    # No competing row was created.
    assert decision_count == 2


# ---------------------------------------------------------------------------
# Job-pipeline SQL layer tests
# ---------------------------------------------------------------------------


def test_claim_next_job_returns_none_when_queue_empty(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    assert db.claim_next_job("w1") is None


def test_claim_next_job_claims_oldest_queued_job_first(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Claim Oldest", None)
    with db.connect() as conn:
        older_id = _insert_job(
            conn, pid, db, created_at="2024-01-01T00:00:00+00:00"
        )
        _insert_job(conn, pid, db, created_at="2024-01-02T00:00:00+00:00")

    row = db.claim_next_job("w1")
    assert row is not None
    assert row["id"] == older_id
    assert row["status"] == "running"
    assert row["worker_id"] == "w1"
    assert row["started_at"] is not None
    assert row["heartbeat_at"] is not None


def test_claim_next_job_race_second_caller_gets_none(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Race", None)
    with db.connect() as conn:
        job_id = _insert_job(conn, pid, db)

    row1 = db.claim_next_job("w1")
    assert row1 is not None
    assert row1["id"] == job_id

    row2 = db.claim_next_job("w2")
    assert row2 is None


def test_set_job_phase_updates_phase_and_heartbeat(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Phase", None)
    with db.connect() as conn:
        job_id = _insert_job(conn, pid, db)
        conn.execute(
            "UPDATE jobs SET heartbeat_at = '2024-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )

    db.set_job_phase(job_id, "parsing")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT phase, heartbeat_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row["phase"] == "parsing"
    assert row["heartbeat_at"] != "2024-01-01T00:00:00+00:00"


def test_complete_ingest_job_marks_item_ready_and_job_done(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Complete Ingest", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(
            conn, pid, db, kind="ingest", status="running", item_id=item_id
        )

    db.complete_ingest_job(job_id, item_id, "My Title")

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert item["status"] == "ready"
    assert item["title"] == "My Title"
    assert item["updated_at"] is not None
    assert job["status"] == "done"
    assert job["finished_at"] is not None
    assert job["heartbeat_at"] is not None


def test_complete_ingest_job_sets_media_fields_when_provided(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Complete Ingest Media", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(
            conn, pid, db, kind="ingest", status="running", item_id=item_id
        )

    db.complete_ingest_job(
        job_id,
        item_id,
        "Media Title",
        media_id="full123",
        thumb_media_id="thumb456",
        thumb_w=800,
        thumb_h=600,
    )

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "ready"
    assert item["title"] == "Media Title"
    assert item["media_id"] == "full123"
    assert item["thumb_media_id"] == "thumb456"
    assert item["thumb_w"] == 800
    assert item["thumb_h"] == 600


def test_complete_ingest_job_leaves_media_fields_null_by_default(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Complete Ingest No Media", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(
            conn, pid, db, kind="ingest", status="running", item_id=item_id
        )

    db.complete_ingest_job(job_id, item_id, "Plain Title")

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "ready"
    assert item["title"] == "Plain Title"
    assert item["media_id"] is None
    assert item["thumb_media_id"] is None
    assert item["thumb_w"] is None
    assert item["thumb_h"] is None


def test_complete_ingest_job_writes_tag_and_note_md_when_provided(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Complete Ingest Tag Note", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(conn, pid, db, kind="ingest", status="running", item_id=item_id)

    db.complete_ingest_job(
        job_id,
        item_id,
        "A Title",
        tag="moody-teal",
        note_md="Some analysis prose.",
    )

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "ready"
    assert item["title"] == "A Title"
    assert item["tag"] == "moody-teal"
    assert item["note_md"] == "Some analysis prose."


def test_complete_job_marks_job_done_without_touching_items_or_syntheses(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Complete Job", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(conn, pid, db, status="running")
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "created_at) VALUES (?, 1, 'dir', '[]', ?)",
            (pid, db._now()),
        )

    db.complete_job(job_id)

    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        item = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        synth_count = conn.execute(
            "SELECT COUNT(*) AS n FROM syntheses WHERE project_id = ?", (pid,)
        ).fetchone()["n"]
    assert job["status"] == "done"
    assert job["finished_at"] is not None
    assert item["status"] == "todo"
    assert synth_count == 1


def test_chain_resynthesize_job_debounces_across_three_calls(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Resynth Debounce", None)

    db.chain_resynthesize_job(pid)
    db.chain_resynthesize_job(pid)
    db.chain_resynthesize_job(pid)

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()["n"]
    assert count == 1


def test_chain_resynthesize_job_debounces_against_running_not_just_queued(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Resynth Running", None)
    with db.connect() as conn:
        _insert_job(conn, pid, db, kind="resynthesize", status="running")

    db.chain_resynthesize_job(pid)

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()["n"]
    assert count == 1


def test_chain_resynthesize_job_inserts_fresh_after_prior_done(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Resynth Fresh", None)
    with db.connect() as conn:
        _insert_job(conn, pid, db, kind="resynthesize", status="done")

    db.chain_resynthesize_job(pid)

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()["n"]
    assert count == 2


def test_chain_resynthesize_job_stores_trigger_item_id(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Trigger Item", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)

    db.chain_resynthesize_job(pid, trigger_item_id=item_id)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT trigger_item_id FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()
    assert row["trigger_item_id"] == item_id


def test_chain_resynthesize_job_defaults_trigger_item_id_to_none_when_omitted(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("No Trigger", None)

    db.chain_resynthesize_job(pid)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT trigger_item_id FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()
    assert row["trigger_item_id"] is None


def test_chain_resynthesize_job_debounce_does_not_overwrite_existing_trigger_item_id(
    app_env,
):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Debounce Trigger", None)

    db.chain_resynthesize_job(pid, trigger_item_id=1)
    db.chain_resynthesize_job(pid, trigger_item_id=2)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT trigger_item_id FROM jobs WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["trigger_item_id"] == 1


def test_resynthesize_job_trigger_item_id_is_independent_of_item_id_column(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Independent", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)

    db.chain_resynthesize_job(pid, trigger_item_id=item_id)

    with db.connect() as conn:
        job = conn.execute(
            "SELECT item_id, trigger_item_id FROM jobs "
            "WHERE project_id = ? AND kind = 'resynthesize'",
            (pid,),
        ).fetchone()

    assert job["item_id"] is None
    assert job["trigger_item_id"] == item_id
    assert db.live_jobs_by_item(pid) == {}


def test_init_db_migrates_trigger_item_id_column(app_env):
    import db

    importlib.reload(db)
    db.init_db()  # ensure DATA_DIR and DB file exist before connect()
    # Simulate an existing DB whose jobs table predates trigger_item_id.
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                item_id INTEGER,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                worker_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                heartbeat_at TEXT,
                finished_at TEXT
            )
            """
        )
    db.init_db()

    with db.connect() as conn:
        cols = {
            row["name"]: row["type"]
            for row in conn.execute("PRAGMA table_info(jobs)")
        }
    assert "trigger_item_id" in cols
    assert cols["trigger_item_id"] == "INTEGER"


def test_fail_job_with_item_id_marks_both_job_and_item_failed(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Fail Both", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(
            conn, pid, db, kind="ingest", status="running", item_id=item_id
        )

    db.fail_job(job_id, item_id, "boom")

    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["finished_at"] is not None
    assert item["status"] == "failed"
    assert item["error"] == "boom"
    assert item["updated_at"] is not None


def test_fail_job_with_none_item_id_only_touches_job(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Fail Job Only", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)
        job_id = _insert_job(
            conn, pid, db, kind="resynthesize", status="running"
        )

    db.fail_job(job_id, None, "boom")  # must not raise

    with db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        item = conn.execute(
            "SELECT status, error FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert item["status"] == "todo"
    assert item["error"] is None


def test_find_stale_running_jobs_only_returns_jobs_past_the_cutoff(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Stale", None)
    with db.connect() as conn:
        stale_id = _insert_job(conn, pid, db, status="running")
        fresh_id = _insert_job(conn, pid, db, status="running")
        conn.execute(
            "UPDATE jobs SET heartbeat_at = '2024-01-01T00:00:00+00:00' WHERE id = ?",
            (stale_id,),
        )
        conn.execute(
            "UPDATE jobs SET heartbeat_at = '2024-12-31T23:59:59+00:00' WHERE id = ?",
            (fresh_id,),
        )

    rows = db.find_stale_running_jobs("2024-06-15T00:00:00+00:00")

    assert [row["id"] for row in rows] == [stale_id]


def test_find_all_running_jobs_returns_every_running_job_regardless_of_heartbeat(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("All Running", None)
    with db.connect() as conn:
        a = _insert_job(conn, pid, db, status="running")
        b = _insert_job(conn, pid, db, status="running")
        conn.execute(
            "UPDATE jobs SET heartbeat_at = '2024-01-01T00:00:00+00:00' WHERE id = ?",
            (a,),
        )
        conn.execute(
            "UPDATE jobs SET heartbeat_at = '2024-12-31T23:59:59+00:00' WHERE id = ?",
            (b,),
        )

    rows = db.find_all_running_jobs()

    assert {row["id"] for row in rows} == {a, b}


def test_create_note_and_ingest_job_inserts_at_next_position(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Note Next Pos", None)
    with db.connect() as conn:
        existing = _insert_item(conn, pid, db, position=0)

    item_id, job_id = db.create_note_and_ingest_job(pid, "hello world")

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert item_id != existing
    assert item["project_id"] == pid
    assert item["kind"] == "note"
    assert item["status"] == "pending"
    assert item["raw_text"] == "hello world"
    assert item["position"] == 1
    assert job["project_id"] == pid
    assert job["item_id"] == item_id
    assert job["kind"] == "ingest"
    assert job["status"] == "queued"


def test_create_note_and_ingest_job_position_zero_when_project_has_no_items(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Note Zero", None)

    item_id, job_id = db.create_note_and_ingest_job(pid, "first")

    with db.connect() as conn:
        item = conn.execute(
            "SELECT position FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert item["position"] == 0


def test_create_url_and_ingest_job_creates_url_kind_item_with_source_url(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("URL Drop", None)

    item_id, job_id = db.create_url_and_ingest_job(pid, "https://example.com/page")

    with db.connect() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert item["project_id"] == pid
    assert item["kind"] == "url"
    assert item["status"] == "pending"
    assert item["source_url"] == "https://example.com/page"
    assert item["raw_text"] is None
    assert job["project_id"] == pid
    assert job["item_id"] == item_id
    assert job["kind"] == "ingest"
    assert job["status"] == "queued"


def test_create_url_and_ingest_job_position_zero_when_project_has_no_items(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("URL Zero", None)

    item_id, job_id = db.create_url_and_ingest_job(pid, "https://example.com/first")

    with db.connect() as conn:
        item = conn.execute(
            "SELECT position FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert item["position"] == 0


def test_list_active_jobs_only_returns_queued_and_running_for_the_given_project(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid_a, _ = db.create_project("Active A", None)
    pid_b, _ = db.create_project("Active B", None)
    with db.connect() as conn:
        queued_a = _insert_job(
            conn, pid_a, db, status="queued", created_at="2024-01-01T00:00:00+00:00"
        )
        running_a = _insert_job(
            conn, pid_a, db, status="running", created_at="2024-01-02T00:00:00+00:00"
        )
        _insert_job(
            conn, pid_a, db, status="done", created_at="2024-01-03T00:00:00+00:00"
        )
        _insert_job(
            conn, pid_a, db, status="failed", created_at="2024-01-04T00:00:00+00:00"
        )
        _insert_job(
            conn, pid_b, db, status="queued", created_at="2024-01-05T00:00:00+00:00"
        )

    rows = db.list_active_jobs(pid_a)

    assert [row["id"] for row in rows] == [queued_a, running_a]
    for row in rows:
        assert row["status"] in ("queued", "running")


def test_items_rev_stable_when_unchanged_and_changes_on_item_insert_or_update(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Rev", None)
    with db.connect() as conn:
        item_id = _insert_item(conn, pid, db, position=0)

    rev1 = db.items_rev(pid)
    rev2 = db.items_rev(pid)
    assert rev1 == rev2

    with db.connect() as conn:
        _insert_item(conn, pid, db, position=1)

    rev3 = db.items_rev(pid)
    assert rev3 != rev1

    db.update_item(item_id, title="new title")

    rev4 = db.items_rev(pid)
    assert rev4 != rev3


def test_latest_synthesis_version_zero_when_none_then_reflects_max(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Ver", None)
    assert db.latest_synthesis_version(pid) == 0
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "created_at) VALUES (?, 3, 'd', '[]', ?)",
            (pid, db._now()),
        )
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "created_at) VALUES (?, 5, 'd', '[]', ?)",
            (pid, db._now()),
        )
    assert db.latest_synthesis_version(pid) == 5


def test_live_jobs_by_item_maps_item_to_its_live_job_and_excludes_jobs_with_null_item_id(
    app_env,
):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Live Map", None)
    with db.connect() as conn:
        item_a = _insert_item(conn, pid, db, position=0)
        item_b = _insert_item(conn, pid, db, position=1)
        running_a = _insert_job(
            conn, pid, db, kind="ingest", status="running", item_id=item_a
        )
        _insert_job(conn, pid, db, kind="ingest", status="done", item_id=item_b)
        _insert_job(conn, pid, db, kind="resynthesize", status="running")

    mapping = db.live_jobs_by_item(pid)

    assert set(mapping.keys()) == {item_a}
    assert mapping[item_a]["id"] == running_a
    assert mapping[item_a]["status"] == "running"


def test_insert_media_inserts_row_matching_given_fields(app_env):
    import db

    importlib.reload(db)
    db.init_db()

    db.insert_media(
        media_id="aabbccdd11223344",
        path="2024/01/image.jpg",
        mime="image/jpeg",
        width=1200,
        height=900,
        byte_size=45678,
    )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM media WHERE id = ?", ("aabbccdd11223344",)
        ).fetchone()
    assert row["path"] == "2024/01/image.jpg"
    assert row["mime"] == "image/jpeg"
    assert row["width"] == 1200
    assert row["height"] == 900
    assert row["byte_size"] == 45678
    assert row["created_at"] is not None


# ---------------------------------------------------------------------------
# Resynthesis context / write tests
# ---------------------------------------------------------------------------


def test_get_resynthesis_context_only_ready_items(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Ready Filter", None)
    with db.connect() as conn:
        _insert_ready_item(conn, pid, db, title="R1")
        _insert_ready_item(conn, pid, db, title="R2")
        _insert_ready_item(conn, pid, db, title="R3")
        conn.execute(
            "INSERT INTO items (project_id, kind, status, position, created_at, updated_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?)",
            (pid, "note", db._now(), db._now()),
        )
        conn.execute(
            "INSERT INTO items (project_id, kind, status, position, created_at, updated_at) "
            "VALUES (?, ?, 'failed', 0, ?, ?)",
            (pid, "note", db._now(), db._now()),
        )

    ctx = db.get_resynthesis_context(pid)

    assert ctx["item_count"] == 3
    assert {it["title"] for it in ctx["items"]} == {"R1", "R2", "R3"}


def test_get_resynthesis_context_caps_at_25_and_orders_oldest_first(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Cap Order", None)
    with db.connect() as conn:
        for i in range(30):
            _insert_ready_item(
                conn,
                pid,
                db,
                title=f"Item {i}",
                updated_at=f"2026-01-01T00:{i:02d}:00+00:00",
            )

    ctx = db.get_resynthesis_context(pid)

    assert len(ctx["items"]) == 25
    assert ctx["item_count"] == 25
    assert ctx["items"][0]["title"] == "Item 5"
    assert ctx["items"][-1]["title"] == "Item 29"
    present = {it["title"] for it in ctx["items"]}
    for excluded in (f"Item {i}" for i in range(5)):
        assert excluded not in present
    for included in (f"Item {i}" for i in range(5, 30)):
        assert included in present


def test_get_resynthesis_context_swatches_attach_in_order(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Swatches", None)
    with db.connect() as conn:
        item_id = _insert_ready_item(conn, pid, db, title="Swatched")

    db.replace_swatches(
        item_id,
        [{"hex": "#111111", "label": "a"}, {"hex": "#222222", "label": "b"}],
    )

    ctx = db.get_resynthesis_context(pid)

    assert len(ctx["items"]) == 1
    assert ctx["items"][0]["swatches"] == [
        {"hex": "#111111", "label": "a"},
        {"hex": "#222222", "label": "b"},
    ]


def test_get_resynthesis_context_accepted_decisions_only_live(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Decisions", None)
    live_id = db.insert_decision(pid, "Live decision", "rationale live")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
            "created_at) VALUES (?, ?, ?, 'agent', 'proposed', ?)",
            (pid, "Proposed decision", "rationale proposed", db._now()),
        )
        cur = conn.execute(
            "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
            "created_at) VALUES (?, ?, ?, 'user', 'accepted', ?)",
            (pid, "Superseded decision", "rationale old", db._now()),
        )
        superseded_id = cur.lastrowid
        conn.execute(
            "UPDATE decisions SET superseded_by = ? WHERE id = ?",
            (live_id, superseded_id),
        )

    ctx = db.get_resynthesis_context(pid)

    assert len(ctx["accepted_decisions"]) == 1
    assert ctx["accepted_decisions"][0]["body_md"] == "Live decision"
    assert ctx["accepted_decisions"][0]["rationale_md"] == "rationale live"


def test_get_resynthesis_context_previous_synthesis_reflects_highest_version(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth History", None)

    ctx0 = db.get_resynthesis_context(pid)
    assert ctx0["previous_synthesis"] is None

    with db.connect() as conn:
        now = db._now()
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "created_at) VALUES (?, 1, 'direction one', '[\"q1\"]', ?)",
            (pid, now),
        )
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "created_at) VALUES (?, 2, 'direction two', '[\"q2\"]', ?)",
            (pid, now),
        )

    ctx = db.get_resynthesis_context(pid)

    assert ctx["previous_synthesis"] is not None
    assert ctx["previous_synthesis"]["version"] == 2
    assert ctx["previous_synthesis"]["direction_md"] == "direction two"
    assert ctx["previous_synthesis"]["questions_json"] == '[\"q2\"]'


def test_get_resynthesis_context_does_not_read_blob_columns(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("No Blobs", None)
    with db.connect() as conn:
        item_id = _insert_ready_item(
            conn,
            pid,
            db,
            title="No Blobs",
            note_md="a note",
            source_url="https://example.com",
            kind="url",
        )
        conn.execute(
            "UPDATE items SET media_id = ?, thumb_media_id = ?, thumb_w = 10, "
            "thumb_h = 20, raw_text = 'raw', alt_text = 'alt' WHERE id = ?",
            ("media-1", "thumb-1", item_id),
        )

    ctx = db.get_resynthesis_context(pid)

    assert set(ctx["items"][0].keys()) == {
        "title",
        "tag",
        "note_md",
        "source_url",
        "kind",
        "swatches",
    }


def test_get_resynthesis_context_project_name_matches(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Specific Name", None)

    ctx = db.get_resynthesis_context(pid)

    assert ctx["project_name"] == "Specific Name"


def test_get_resynthesis_context_empty_project(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Empty", None)

    ctx = db.get_resynthesis_context(pid)

    assert ctx["project_name"] == "Empty"
    assert ctx["item_count"] == 0
    assert ctx["items"] == []
    assert ctx["accepted_decisions"] == []
    assert ctx["previous_synthesis"] is None


def test_propose_decision_creates_proposed_agent_row(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Propose", None)

    did = db.propose_decision(pid, "Body text", "Rationale text", 123)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert row["project_id"] == pid
    assert row["body_md"] == "Body text"
    assert row["rationale_md"] == "Rationale text"
    assert row["source"] == "agent"
    assert row["status"] == "proposed"
    assert row["superseded_by"] is None
    assert row["decided_at"] is None
    assert row["job_id"] == 123


def test_propose_decision_stores_none_rationale(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Propose None", None)

    did = db.propose_decision(pid, "Body only", None, 456)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    assert row["rationale_md"] is None


def test_propose_decision_never_mutates_existing_rows(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Propose Append", None)
    existing_id = db.insert_decision(pid, "Original", "Original rationale")
    with db.connect() as conn:
        snapshot = dict(
            conn.execute("SELECT * FROM decisions WHERE id = ?", (existing_id,)).fetchone()
        )

    did1 = db.propose_decision(pid, "Proposal 1", None, 1)
    did2 = db.propose_decision(pid, "Proposal 2", "r2", 2)

    with db.connect() as conn:
        existing = dict(
            conn.execute("SELECT * FROM decisions WHERE id = ?", (existing_id,)).fetchone()
        )
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE project_id = ?", (pid,)
        ).fetchone()["n"]

    assert existing == snapshot
    assert total == 3
    assert did1 != existing_id
    assert did2 != existing_id
    assert did1 != did2


def test_insert_synthesis_first_call_version_one_and_model(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Insert", None)

    sid = db.insert_synthesis(pid, "direction", "[]", 3, None, 7)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM syntheses WHERE id = ?", (sid,)).fetchone()
    assert row["version"] == 1
    assert row["model"] == "claude-sonnet-5"


def test_insert_synthesis_second_call_increments_version_and_preserves_first(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Versions", None)

    sid1 = db.insert_synthesis(pid, "first direction", "[]", 1, None, 1)
    sid2 = db.insert_synthesis(pid, "second direction", "[]", 2, None, 2)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, version, direction_md FROM syntheses "
            "WHERE project_id = ? ORDER BY version",
            (pid,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == sid1
    assert rows[0]["version"] == 1
    assert rows[0]["direction_md"] == "first direction"
    assert rows[1]["id"] == sid2
    assert rows[1]["version"] == 2
    assert rows[1]["direction_md"] == "second direction"


def test_insert_synthesis_stores_fields_exactly(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Fields", None)
    with db.connect() as conn:
        item_id = _insert_ready_item(conn, pid, db, title="Trigger")

    sid = db.insert_synthesis(pid, "dir", '[\"q\"]', 5, item_id, 42)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM syntheses WHERE id = ?", (sid,)).fetchone()
    assert row["direction_md"] == "dir"
    assert row["questions_json"] == '[\"q\"]'
    assert row["item_count"] == 5
    assert row["trigger_item_id"] == item_id
    assert row["job_id"] == 42


def test_insert_synthesis_three_calls_preserve_version_one(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Immutable", None)

    sid1 = db.insert_synthesis(pid, "v1 dir", "[]", 1, None, 1)
    with db.connect() as conn:
        snapshot = dict(
            conn.execute("SELECT * FROM syntheses WHERE id = ?", (sid1,)).fetchone()
        )

    db.insert_synthesis(pid, "v2 dir", "[]", 2, None, 2)
    db.insert_synthesis(pid, "v3 dir", "[]", 3, None, 3)

    with db.connect() as conn:
        v1 = dict(conn.execute("SELECT * FROM syntheses WHERE id = ?", (sid1,)).fetchone())
    assert v1 == snapshot


def test_insert_synthesis_race_safety_under_concurrent_threads(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    pid, _ = db.create_project("Synth Race", None)

    barrier = threading.Barrier(2)
    errors = []

    def worker(direction, job_id):
        try:
            barrier.wait()
            db.insert_synthesis(pid, direction, "[]", 1, None, job_id)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("dir X", 1))
    t2 = threading.Thread(target=worker, args=("dir Y", 2))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT version, direction_md FROM syntheses "
            "WHERE project_id = ? ORDER BY version",
            (pid,),
        ).fetchall()
    versions = [r["version"] for r in rows]
    directions = {r["direction_md"] for r in rows}
    assert len(rows) == 2
    assert set(versions) == {1, 2}
    assert directions == {"dir X", "dir Y"}
