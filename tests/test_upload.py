from __future__ import annotations

import re
from io import BytesIO
from urllib.parse import urlparse

from PIL import Image

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


def _make_image_bytes(fmt: str = "JPEG") -> bytes:
    img = Image.new("RGB", (50, 50), "red")
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_upload_creates_image_item_and_job_and_redirects(logged_in_client):
    project_id, slug = db.create_project("Upload Test", None)
    resp = logged_in_client.get(f"/p/{slug}")
    assert resp.status_code == 200
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    img_bytes = _make_image_bytes()
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"file": (BytesIO(img_bytes), "photo.jpg"), "csrf_token": token},
        content_type="multipart/form-data",
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

    assert item["kind"] == "image"
    assert item["status"] == "pending"
    assert item["media_id"] is not None

    with db.connect() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE item_id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
    assert job is not None
    assert job["kind"] == "ingest"
    assert job["status"] == "queued"


def test_upload_without_csrf_token_is_rejected(logged_in_client):
    project_id, slug = db.create_project("No CSRF", None)
    img_bytes = _make_image_bytes()
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"file": (BytesIO(img_bytes), "photo.jpg")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_upload_no_file_renders_error_and_creates_nothing(logged_in_client):
    project_id, slug = db.create_project("Empty Upload", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    before_items, before_jobs = _counts(project_id)

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"csrf_token": token},  # pragma: allowlist secret
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Choose an image file to upload." in resp.data

    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"file": (BytesIO(b""), ""), "csrf_token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Choose an image file to upload." in resp.data

    after_items, after_jobs = _counts(project_id)
    assert after_items == before_items
    assert after_jobs == before_jobs


def test_upload_svg_disguised_as_jpg_renders_error_and_creates_nothing(logged_in_client):
    project_id, slug = db.create_project("SVG Upload", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    before_items, before_jobs = _counts(project_id)

    svg_bytes = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"file": (BytesIO(svg_bytes), "photo.jpg"), "csrf_token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"SVG images are not supported" in resp.data
    assert b"Traceback" not in resp.data

    after_items, after_jobs = _counts(project_id)
    assert after_items == before_items
    assert after_jobs == before_jobs


def test_upload_on_nonexistent_project_404s(logged_in_client):
    project_id, slug = db.create_project("Token Source", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    img_bytes = _make_image_bytes()
    resp = logged_in_client.post(
        "/api/projects/9999/upload",
        data={"file": (BytesIO(img_bytes), "photo.jpg"), "csrf_token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_upload_on_archived_project_404s_and_creates_nothing(logged_in_client):
    active_id, active_slug = db.create_project("Active Token", None)
    archived_id, _archived_slug = db.create_project("Archived Upload", None)
    _archive_project(archived_id)

    resp = logged_in_client.get(f"/p/{active_slug}")
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    before = _counts(archived_id)
    img_bytes = _make_image_bytes()
    resp = logged_in_client.post(
        f"/api/projects/{archived_id}/upload",
        data={"file": (BytesIO(img_bytes), "photo.jpg"), "csrf_token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert _counts(archived_id) == before


def test_upload_oversized_body_returns_413_without_traceback(logged_in_client):
    project_id, slug = db.create_project("Oversized", None)
    resp = logged_in_client.get(f"/p/{slug}")
    token = _csrf_token(resp.data)  # pragma: allowlist secret

    big = b"x" * (13 * 1024 * 1024)
    resp = logged_in_client.post(
        f"/api/projects/{project_id}/upload",
        data={"file": (BytesIO(big), "big.bin"), "csrf_token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 413
    assert b"Traceback" not in resp.data
    assert b"stack trace" not in resp.data.lower()
