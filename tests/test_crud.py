import re
import secrets

from werkzeug.datastructures import MultiDict

import db


def _csrf_token(html: bytes) -> str:
    m = re.search(rb'csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field"
    return m.group(1).decode()


def _project_id_slug(slug: str) -> tuple[int, str]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, slug FROM projects WHERE slug = ? AND archived_at IS NULL",
            (slug,),
        ).fetchone()
    assert row is not None
    return row["id"], row["slug"]


def _insert_item(project_id: int, title: str | None = None) -> int:
    now = db._now()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO items (project_id, kind, status, position, media_id, "
            "thumb_media_id, created_at, updated_at, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, "test", "todo", 0, None, None, now, now, title),
        )
        return cur.lastrowid


def _archive_project(project_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?",
            (db._now(), project_id),
        )


def test_create_project_redirects_to_new_slug_and_appears_on_board(logged_in_client):
    name = f"CRUD New Project {secrets.token_hex(4)}"
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        "/projects", data={"name": name, "csrf_token": token}  # pragma: allowlist secret
    )
    assert resp.status_code == 303
    location = resp.headers["Location"]
    assert location.startswith("/p/")
    slug = location[len("/p/") :]

    board2 = logged_in_client.get("/")
    assert name.encode() in board2.data

    with db.connect() as conn:
        row = conn.execute(
            "SELECT name, slug FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
    assert row is not None
    assert row["name"] == name
    assert row["slug"] == slug


def test_create_project_slug_collision_appends_suffix(logged_in_client):
    name = f"CRUD Collision {secrets.token_hex(4)}"
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp1 = logged_in_client.post(
        "/projects", data={"name": name, "csrf_token": token}  # pragma: allowlist secret
    )
    assert resp1.status_code == 303
    slug1 = resp1.headers["Location"][len("/p/") :]

    resp2 = logged_in_client.post(
        "/projects", data={"name": name, "csrf_token": token}  # pragma: allowlist secret
    )
    assert resp2.status_code == 303
    slug2 = resp2.headers["Location"][len("/p/") :]
    assert slug2 == f"{slug1}-2"

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT slug FROM projects WHERE slug IN (?, ?)", (slug1, slug2)
        ).fetchall()
    assert {r["slug"] for r in rows} == {slug1, slug2}


def test_create_project_empty_name_rerenders_board_with_error(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        "/projects", data={"name": "", "csrf_token": token}  # pragma: allowlist secret
    )
    assert resp.status_code == 200
    assert b"dt-alert--danger" in resp.data
    assert b"Studio Portfolio Site" in resp.data
    assert b"jemplayer82" in resp.data


def test_create_project_without_csrf_token_is_rejected(logged_in_client):
    name = f"CRUD No CSRF {secrets.token_hex(4)}"
    resp = logged_in_client.post("/projects", data={"name": name})
    assert resp.status_code == 403


def test_item_edit_updates_title_tag_note_and_persists_on_reload(logged_in_client):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id, projects.slug FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "JOIN swatches ON swatches.item_id = items.id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()
        swatches = conn.execute(
            "SELECT hex, label, position FROM swatches "
            "WHERE item_id = ? ORDER BY position",
            (item["id"],),
        ).fetchall()

    item_id = item["id"]
    slug = item["slug"]
    new_title = f"Updated Title {secrets.token_hex(4)}"
    new_tag = f"updated-tag-{secrets.token_hex(4)}"
    new_note = f"Updated note body {secrets.token_hex(4)}"

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    data = MultiDict(
        [
            ("title", new_title),
            ("tag", new_tag),
            ("note_md", new_note),
            ("csrf_token", token),
        ]
    )
    for sw in swatches:
        data.add("swatch_hex", sw["hex"])
        data.add("swatch_label", sw["label"] or "")
    data.add("swatch_hex", "")
    data.add("swatch_label", "")

    resp = logged_in_client.post(f"/api/items/{item_id}/edit", data=data)
    assert resp.status_code == 302
    assert f"#item-{item_id}" in resp.headers["Location"]

    page2 = logged_in_client.get(f"/p/{slug}")
    assert new_title.encode() in page2.data
    assert new_tag.encode() in page2.data
    assert new_note.encode() in page2.data

    with db.connect() as conn:
        row = conn.execute(
            "SELECT title, tag, note_md FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row["title"] == new_title
    assert row["tag"] == new_tag
    assert row["note_md"] == new_note

    with db.connect() as conn:
        after_swatches = conn.execute(
            "SELECT hex, label, position FROM swatches "
            "WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    assert [dict(r) for r in after_swatches] == [dict(r) for r in swatches]


def test_item_edit_rejects_invalid_swatch_hex_and_leaves_existing_swatches_untouched(
    logged_in_client,
):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id, projects.slug FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "JOIN swatches ON swatches.item_id = items.id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT hex, label, position FROM swatches "
            "WHERE item_id = ? ORDER BY position",
            (item["id"],),
        ).fetchall()

    item_id = item["id"]
    slug = item["slug"]

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    data = MultiDict(
        [
            ("title", f"Bad Swatch {secrets.token_hex(4)}"),
            ("tag", "bad-swatch"),
            ("note_md", "note"),
            ("swatch_hex", "not-a-color"),
            ("swatch_label", "Invalid label"),
            ("swatch_hex", ""),
            ("swatch_label", ""),
            ("csrf_token", token),
        ]
    )

    resp = logged_in_client.post(f"/api/items/{item_id}/edit", data=data)
    assert resp.status_code == 200
    assert b"dt-alert--danger" in resp.data

    with db.connect() as conn:
        after = conn.execute(
            "SELECT hex, label, position FROM swatches "
            "WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
    assert [dict(r) for r in after] == [dict(r) for r in snapshot]


def test_item_edit_nonexistent_item_returns_404(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    with db.connect() as conn:
        item_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM items"
        ).fetchone()[0]

    resp = logged_in_client.post(
        f"/api/items/{item_id}/edit",
        data={
            "title": f"Should not apply {secrets.token_hex(4)}",
            "tag": "no-op",
            "note_md": "no-op",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404


def test_item_edit_archived_project_item_returns_404_and_leaves_item_untouched(
    logged_in_client,
):
    project_id, _ = db.create_project(
        f"Archived Edit Project {secrets.token_hex(4)}", None
    )
    item_id = _insert_item(project_id, title="Original title")
    _archive_project(project_id)

    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{item_id}/edit",
        data={
            "title": f"Changed title {secrets.token_hex(4)}",
            "tag": "changed-tag",
            "note_md": "changed note",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        row = conn.execute(
            "SELECT title, tag, note_md, position FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()

    assert row["title"] == "Original title"
    assert row["tag"] is None
    assert row["note_md"] is None
    assert row["position"] == 0


def test_item_delete_removes_item_and_swatches_and_survives_missing_file(
    logged_in_client,
):
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id, slug FROM projects WHERE slug = ?",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()
        item = conn.execute(
            "SELECT items.id, items.thumb_media_id, media.path FROM items "
            "JOIN media ON media.id = items.thumb_media_id "
            "JOIN swatches ON swatches.item_id = items.id "
            "WHERE items.project_id = ? "
            "LIMIT 1",
            (proj["id"],),
        ).fetchone()

    item_id = item["id"]
    media_id = item["thumb_media_id"]
    media_path = item["path"]
    slug = proj["slug"]

    (db.MEDIA_DIR / media_path).unlink()

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{item_id}/delete", data={"csrf_token": token}  # pragma: allowlist secret
    )
    assert resp.status_code == 302

    with db.connect() as conn:
        assert (
            conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM swatches WHERE item_id = ?", (item_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone()
            is None
        )


def test_item_delete_nonexistent_item_returns_404(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    with db.connect() as conn:
        item_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM items"
        ).fetchone()[0]

    resp = logged_in_client.post(
        f"/api/items/{item_id}/delete", data={"csrf_token": token}  # pragma: allowlist secret
    )
    assert resp.status_code == 404


def test_item_delete_archived_project_item_returns_404_and_leaves_item_untouched(
    logged_in_client,
):
    project_id, _ = db.create_project(
        f"Archived Delete Project {secrets.token_hex(4)}", None
    )
    item_id = _insert_item(project_id, title="Item to keep")
    _archive_project(project_id)

    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{item_id}/delete", data={"csrf_token": token}  # pragma: allowlist secret
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, title, position FROM items WHERE id = ?", (item_id,)
        ).fetchone()

    assert row is not None
    assert row["title"] == "Item to keep"
    assert row["position"] == 0


def test_item_move_swaps_positions(logged_in_client):
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id, slug FROM projects WHERE slug = ?",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()
        rows = conn.execute(
            "SELECT id, position FROM items "
            "WHERE project_id = ? "
            "ORDER BY position, id LIMIT 2",
            (proj["id"],),
        ).fetchall()

    first_id, first_pos = rows[0]["id"], rows[0]["position"]
    second_id, second_pos = rows[1]["id"], rows[1]["position"]
    slug = proj["slug"]

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{first_id}/move",
        data={"direction": "down", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 302

    with db.connect() as conn:
        new_first = conn.execute(
            "SELECT position FROM items WHERE id = ?", (first_id,)
        ).fetchone()["position"]
        new_second = conn.execute(
            "SELECT position FROM items WHERE id = ?", (second_id,)
        ).fetchone()["position"]

    assert new_first == second_pos
    assert new_second == first_pos


def test_item_move_tie_position_swaps_order(logged_in_client):
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id, slug FROM projects WHERE slug = ?",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()
        rows = conn.execute(
            "SELECT id, position FROM items "
            "WHERE project_id = ? "
            "ORDER BY id LIMIT 2",
            (proj["id"],),
        ).fetchall()

    first_id, second_id = rows[0]["id"], rows[1]["id"]
    shared_position = rows[0]["position"]
    slug = proj["slug"]

    with db.connect() as conn:
        conn.execute(
            "UPDATE items SET position = ?, updated_at = ? WHERE id IN (?, ?)",
            (shared_position, db._now(), first_id, second_id),
        )

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{first_id}/move",
        data={"direction": "down", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 302

    with db.connect() as conn:
        first_pos = conn.execute(
            "SELECT position FROM items WHERE id = ?", (first_id,)
        ).fetchone()["position"]
        second_pos = conn.execute(
            "SELECT position FROM items WHERE id = ?", (second_id,)
        ).fetchone()["position"]

    assert first_pos != second_pos
    assert first_pos > second_pos


def test_item_move_invalid_direction_returns_400(logged_in_client):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()

    page = logged_in_client.get("/")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{item['id']}/move",
        data={"direction": "sideways", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 400


def test_item_move_nonexistent_item_returns_404(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    with db.connect() as conn:
        item_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM items"
        ).fetchone()[0]

    resp = logged_in_client.post(
        f"/api/items/{item_id}/move",
        data={"direction": "down", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 404


def test_item_move_archived_project_item_returns_404_and_leaves_item_untouched(
    logged_in_client,
):
    project_id, _ = db.create_project(
        f"Archived Move Project {secrets.token_hex(4)}", None
    )
    item_id = _insert_item(project_id)
    _archive_project(project_id)

    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/items/{item_id}/move",
        data={"direction": "down", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, position FROM items WHERE id = ?", (item_id,)
        ).fetchone()

    assert row is not None
    assert row["position"] == 0


def test_item_edit_without_csrf_token_is_rejected(logged_in_client):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()

    resp = logged_in_client.post(
        f"/api/items/{item['id']}/edit",
        data={"title": "x", "tag": "y", "note_md": "z"},
    )
    assert resp.status_code == 403


def test_item_delete_without_csrf_token_is_rejected(logged_in_client):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()

    resp = logged_in_client.post(f"/api/items/{item['id']}/delete")
    assert resp.status_code == 403


def test_item_move_without_csrf_token_is_rejected(logged_in_client):
    with db.connect() as conn:
        item = conn.execute(
            "SELECT items.id FROM items "
            "JOIN projects ON projects.id = items.project_id "
            "WHERE projects.slug = ? "
            "LIMIT 1",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()

    resp = logged_in_client.post(
        f"/api/items/{item['id']}/move", data={"direction": "down"}
    )
    assert resp.status_code == 403


def test_add_decision_inserts_accepted_row(logged_in_client):
    project_id, slug = _project_id_slug("jemplayer82-web-design-ideas")

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    body = f"Decision body {secrets.token_hex(4)}"
    rationale = f"Rationale {secrets.token_hex(4)}"

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions",
        data={"body_md": body, "rationale_md": rationale, "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/p/{slug}"

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, body_md, rationale_md, status, source, "
            "superseded_by, decided_at FROM decisions "
            "WHERE body_md = ?",
            (body,),
        ).fetchone()

    assert row is not None
    assert row["body_md"] == body
    assert row["rationale_md"] == rationale
    assert row["status"] == "accepted"
    assert row["source"] == "user"
    assert row["superseded_by"] is None
    assert row["decided_at"] is not None

    page2 = logged_in_client.get(f"/p/{slug}")
    assert body.encode() in page2.data


def test_edit_decision_creates_new_row_and_supersedes_old_without_mutating_it(
    logged_in_client,
):
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id, slug FROM projects WHERE slug = ?", ("studio-portfolio-site",)
        ).fetchone()
        old = conn.execute(
            "SELECT id, body_md FROM decisions "
            "WHERE project_id = ? AND status = 'accepted' AND superseded_by IS NULL "
            "LIMIT 1",
            (proj["id"],),
        ).fetchone()

    assert old is not None
    old_id = old["id"]
    original_body = old["body_md"]
    new_body = f"Updated decision {secrets.token_hex(4)}"
    new_rationale = f"Updated rationale {secrets.token_hex(4)}"

    page = logged_in_client.get(f"/p/{proj['slug']}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/projects/{proj['id']}/decisions/{old_id}/edit",
        data={"body_md": new_body, "rationale_md": new_rationale, "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 302

    with db.connect() as conn:
        old_row = conn.execute(
            "SELECT body_md, superseded_by FROM decisions WHERE id = ?", (old_id,)
        ).fetchone()
        assert old_row["body_md"] == original_body
        assert old_row["superseded_by"] is not None

        new_id = old_row["superseded_by"]
        new_row = conn.execute(
            "SELECT body_md, rationale_md, status, superseded_by FROM decisions "
            "WHERE id = ?",
            (new_id,),
        ).fetchone()

    assert new_row["body_md"] == new_body
    assert new_row["rationale_md"] == new_rationale
    assert new_row["status"] == "accepted"
    assert new_row["superseded_by"] is None

    page2 = logged_in_client.get(f"/p/{proj['slug']}")
    assert new_body.encode() in page2.data


def test_add_decision_empty_body_rerenders_with_error(logged_in_client):
    project_id, slug = _project_id_slug("jemplayer82-web-design-ideas")

    page = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions",
        data={"body_md": "", "rationale_md": "rationale", "csrf_token": token},  # pragma: allowlist secret
    )
    assert resp.status_code == 200
    assert b"dt-alert--danger" in resp.data


def test_decision_create_nonexistent_project_returns_404(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    with db.connect() as conn:
        project_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM projects"
        ).fetchone()[0]

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions",
        data={
            "body_md": f"Body {secrets.token_hex(4)}",
            "rationale_md": f"Rationale {secrets.token_hex(4)}",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404


def test_decision_create_archived_project_returns_404_and_leaves_decisions_untouched(
    logged_in_client,
):
    project_id, _ = db.create_project(
        f"Archived Decision Project {secrets.token_hex(4)}", None
    )
    _archive_project(project_id)

    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions",
        data={
            "body_md": f"Body {secrets.token_hex(4)}",
            "rationale_md": f"Rationale {secrets.token_hex(4)}",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE project_id = ?", (project_id,)
        ).fetchone()[0]

    assert count == 0


def test_decision_create_without_csrf_token_is_rejected(logged_in_client):
    project_id, _ = _project_id_slug("jemplayer82-web-design-ideas")

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions",
        data={"body_md": "body", "rationale_md": "rationale"},
    )
    assert resp.status_code == 403


def test_decision_edit_without_csrf_token_is_rejected(logged_in_client):
    with db.connect() as conn:
        proj = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", ("studio-portfolio-site",)
        ).fetchone()
        decision = conn.execute(
            "SELECT id FROM decisions "
            "WHERE project_id = ? AND status = 'accepted' AND superseded_by IS NULL "
            "LIMIT 1",
            (proj["id"],),
        ).fetchone()

    resp = logged_in_client.post(
        f"/api/projects/{proj['id']}/decisions/{decision['id']}/edit",
        data={"body_md": "body", "rationale_md": "rationale"},
    )
    assert resp.status_code == 403


def test_decision_edit_cross_project_returns_404_and_leaves_row_untouched(
    logged_in_client,
):
    pid_a, slug_a = db.create_project("Project A Cross", None)
    pid_b, _slug_b = db.create_project("Project B Cross", None)
    original_body = f"Original decision {secrets.token_hex(4)}"
    decision_id = db.insert_decision(pid_a, original_body, None)

    page = logged_in_client.get(f"/p/{slug_a}")
    token = _csrf_token(page.data)  # pragma: allowlist secret
    new_body = f"Updated decision {secrets.token_hex(4)}"
    new_rationale = f"Updated rationale {secrets.token_hex(4)}"

    resp = logged_in_client.post(
        f"/api/projects/{pid_b}/decisions/{decision_id}/edit",
        data={
            "body_md": new_body,
            "rationale_md": new_rationale,
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        row = conn.execute(
            "SELECT project_id, body_md, superseded_by FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()

    assert row["project_id"] == pid_a
    assert row["body_md"] == original_body
    assert row["superseded_by"] is None


def test_decision_edit_nonexistent_project_returns_404(logged_in_client):
    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    with db.connect() as conn:
        project_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM projects"
        ).fetchone()[0]

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions/1/edit",
        data={
            "body_md": f"Body {secrets.token_hex(4)}",
            "rationale_md": f"Rationale {secrets.token_hex(4)}",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404


def test_decision_edit_archived_project_returns_404_and_leaves_decision_untouched(
    logged_in_client,
):
    project_id, _ = db.create_project(
        f"Archived Decision Edit Project {secrets.token_hex(4)}", None
    )
    original_body = f"Original body {secrets.token_hex(4)}"
    original_rationale = f"Original rationale {secrets.token_hex(4)}"
    decision_id = db.insert_decision(project_id, original_body, original_rationale)
    _archive_project(project_id)

    board = logged_in_client.get("/")
    token = _csrf_token(board.data)  # pragma: allowlist secret
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/decisions/{decision_id}/edit",
        data={
            "body_md": f"Changed body {secrets.token_hex(4)}",
            "rationale_md": f"Changed rationale {secrets.token_hex(4)}",
            "csrf_token": token,  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 404

    with db.connect() as conn:
        row = conn.execute(
            "SELECT body_md, rationale_md, superseded_by FROM decisions "
            "WHERE id = ?",
            (decision_id,),
        ).fetchone()

    assert row["body_md"] == original_body
    assert row["rationale_md"] == original_rationale
    assert row["superseded_by"] is None