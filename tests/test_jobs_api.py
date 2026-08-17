from __future__ import annotations

import re
from urllib.parse import urlparse

import db


def _csrf_token(html: bytes) -> str:
    m = re.search(rb'csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field"
    return m.group(1).decode()


def _archive_project(project_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?",
            (db._now(), project_id),
        )


def _counts(project_id: int) -> tuple[int, int]:
    with db.connect() as conn:
        item_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        job_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
    return item_count, job_count


def _table_snapshot(project_id: int, table: str) -> list[tuple]:
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [tuple(r) for r in rows]


def test_drop_creates_item_and_job_and_redirects_to_item_anchor(logged_in_client):
    project_id, slug = db.create_project("Drop Test", None)
    resp = logged_in_client.get(f"/p/{slug}")
    assert resp.status_code == 200
    token = _csrf_token(resp.data)

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/drop",
        data={"raw_text": "Hello note", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db.connect() as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    assert item is not None
    item_id = item["id"]

    parsed = urlparse(resp.headers["Location"])
    assert parsed.path == f"/p/{slug}"
    assert parsed.fragment == f"item-{item_id}"

    assert item["kind"] == "note"
    assert item["status"] == "pending"
    assert item["raw_text"] == "Hello note"

    with db.connect() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE item_id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
    assert job is not None
    assert job["kind"] == "ingest"
    assert job["status"] == "queued"
    assert job["project_id"] == project_id


def test_drop_on_nonexistent_project_404s(logged_in_client):
    project_id, slug = db.create_project("Token Source", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)

    resp = logged_in_client.post(
        "/api/projects/9999/drop",
        data={"raw_text": "note", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_drop_on_archived_project_404s_and_creates_nothing(logged_in_client):
    active_id, active_slug = db.create_project("Active Token", None)
    archived_id, _archived_slug = db.create_project("Archived Drop", None)
    _archive_project(archived_id)

    resp = logged_in_client.get(f"/p/{active_slug}")
    token = _csrf_token(resp.data)

    before = _counts(archived_id)
    resp = logged_in_client.post(
        f"/api/projects/{archived_id}/drop",
        data={"raw_text": "note", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert _counts(archived_id) == before


def test_drop_without_csrf_token_is_rejected(logged_in_client):
    project_id, slug = db.create_project("No CSRF", None)
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/drop",
        data={"raw_text": "note"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_drop_empty_raw_text_rerenders_with_error_and_creates_nothing(logged_in_client):
    project_id, slug = db.create_project("Empty Drop", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)

    before_items, before_jobs = _counts(project_id)
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/drop",
        data={"raw_text": "   ", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"A note is required." in resp.data

    after_items, after_jobs = _counts(project_id)
    assert after_items == before_items
    assert after_jobs == before_jobs


def test_status_returns_expected_shape_and_only_queued_and_running_jobs(logged_in_client):
    project_id, slug = db.create_project("Status Test", None)
    now = db._now()
    with db.connect() as conn:
        for status in ("queued", "running", "done", "failed"):
            conn.execute(
                "INSERT INTO jobs "
                "(project_id, item_id, kind, status, phase, created_at) "
                "VALUES (?, NULL, 'ingest', ?, ?, ?)",
                (project_id, status, f"phase-{status}", now),
            )

    resp = logged_in_client.get(f"/api/projects/{project_id}/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) >= {"jobs", "item_rev", "syn_version"}
    assert data["syn_version"] == 0

    jobs = data["jobs"]
    assert len(jobs) == 2
    statuses = {j["status"] for j in jobs}
    assert statuses == {"queued", "running"}
    for j in jobs:
        assert set(j.keys()) == {"id", "kind", "status", "phase"}
        assert j["kind"] == "ingest"
        assert j["phase"] == f"phase-{j['status']}"


def test_status_has_cache_control_no_store(logged_in_client):
    project_id, slug = db.create_project("Cache Test", None)
    resp = logged_in_client.get(f"/api/projects/{project_id}/status")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_status_causes_zero_db_writes(logged_in_client):
    project_id, slug = db.create_project("Zero Writes", None)
    now = db._now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs "
            "(project_id, item_id, kind, status, phase, created_at) "
            "VALUES (?, NULL, 'ingest', 'queued', 'test', ?)",
            (project_id, now),
        )

    before = {
        "jobs": _table_snapshot(project_id, "jobs"),
        "items": _table_snapshot(project_id, "items"),
        "syntheses": _table_snapshot(project_id, "syntheses"),
    }
    resp = logged_in_client.get(f"/api/projects/{project_id}/status")
    assert resp.status_code == 200
    after = {
        "jobs": _table_snapshot(project_id, "jobs"),
        "items": _table_snapshot(project_id, "items"),
        "syntheses": _table_snapshot(project_id, "syntheses"),
    }
    assert after == before


def test_status_on_nonexistent_project_404s(logged_in_client):
    resp = logged_in_client.get("/api/projects/9999/status")
    assert resp.status_code == 404


def test_status_on_archived_project_404s(logged_in_client):
    project_id, slug = db.create_project("Archived Status", None)
    _archive_project(project_id)
    resp = logged_in_client.get(f"/api/projects/{project_id}/status")
    assert resp.status_code == 404


def test_status_requires_auth(client):
    resp = client.get("/api/projects/1/status")
    assert resp.status_code == 401


def test_project_page_renders_drop_form(logged_in_client):
    project_id, slug = db.create_project("Drop Form", None)
    resp = logged_in_client.get(f"/p/{slug}")
    assert resp.status_code == 200
    assert b'/drop"' in resp.data
    assert b'name="raw_text"' in resp.data


def test_project_page_renders_job_status_badge(logged_in_client):
    project_id, slug = db.create_project("Badge Test", None)
    item_id, job_id = db.create_note_and_ingest_job(project_id, "pending note")

    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='running', phase='analyze' WHERE id=?",
            (job_id,),
        )

    resp = logged_in_client.get(f"/p/{slug}")
    assert resp.status_code == 200
    assert b'data-job-status="running"' in resp.data
    assert f'data-job-id="{job_id}"'.encode() in resp.data
    assert b"analyze" in resp.data
