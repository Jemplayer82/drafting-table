from __future__ import annotations

import importlib


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