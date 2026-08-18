"""Tests for worker.py.

These tests exercise the separate-process job worker without importing
Flask or app.py.
"""

from __future__ import annotations

import datetime
import http.server
import importlib
import ipaddress
import socket as socket_module
from io import BytesIO
from urllib.parse import urlsplit

import pytest
from PIL import Image

import agent
import db
import net_guard
import worker


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _stale_iso() -> str:
    delta = datetime.timedelta(seconds=worker.HEARTBEAT_STALE_SECONDS + 1)
    return (datetime.datetime.now(datetime.UTC) - delta).isoformat()


def _noop_sleep(*_):
    return None


def _insert_item(
    conn,
    *,
    project_id,
    raw_text=None,
    source_url=None,
    kind="note",
    status="pending",
    title=None,
    error=None,
):
    cur = conn.execute(
        """
        INSERT INTO items (project_id, kind, raw_text, source_url, status,
                           title, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            kind,
            raw_text,
            source_url,
            status,
            title,
            error,
            _now_iso(),
            _now_iso(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _insert_job(
    conn,
    *,
    kind,
    project_id,
    item_id,
    status="queued",
    phase=None,
    heartbeat_at=None,
    error=None,
    created_at=None,
):
    if created_at is None:
        created_at = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO jobs (kind, status, project_id, item_id, phase,
                          heartbeat_at, created_at, finished_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind,
            status,
            project_id,
            item_id,
            phase,
            heartbeat_at,
            created_at,
            None,
            error,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _make_jpeg_bytes() -> bytes:
    img = Image.new("RGB", (300, 200), color=(120, 180, 220))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


_TEST_JPEG = _make_jpeg_bytes()


def _make_large_jpeg_bytes() -> bytes:
    img = Image.new("RGB", (2000, 1400), color=(120, 180, 220))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


_TEST_LARGE_JPEG = _make_large_jpeg_bytes()


class _PageWithOgImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page":
            body = (
                b"<html><head><title>Cool Reference</title>"
                b'<meta property="og:image" content="/img.jpg"></head>'
                b"<body></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/img.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(_TEST_LARGE_JPEG)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class _PageWithoutOgImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>No Image Here</title></head><body></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _PageWithBadOgImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page":
            body = (
                b"<html><head><title>Broken Image Page</title>"
                b'<meta property="og:image" content="/not-image.bin"></head></html>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/not-image.bin":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not actually an image")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class _PageWithPrivateOgImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            b"<html><head><title>Private Image Page</title>"
            b'<meta property="og:image" content="http://192.168.1.1/evil.png"></head></html>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _PageWithMalformedOgImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            b"<html><head><title>Malformed Image Page</title>"
            b'<meta property="og:image" content="http://[::1/x"></head></html>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _NotFoundHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, *a):
        pass


@pytest.fixture(autouse=True)
def _fresh_db(app_env):
    importlib.reload(db)
    db.init_db()
    importlib.reload(worker)


@pytest.fixture(autouse=True)
def _mock_analyze_item(monkeypatch, _fresh_db):
    def _fake(*, title_hint=None, url=None, page_text=None, user_note=None):
        return {
            "title": title_hint or "Mock Title",
            "tag": "mock-tag",
            "note": "Mock analysis note.",
            "swatches": [],
            "confidence": "medium",
        }

    monkeypatch.setattr(agent, "analyze_item", _fake)


def test_claim_race_two_workers_only_one_wins():
    with db.connect() as conn:
        project_id, _ = db.create_project("race", "")
        item_id = _insert_item(conn, project_id=project_id, raw_text="note")
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)

    first = db.claim_next_job("worker-a")
    second = db.claim_next_job("worker-b")
    assert first is not None
    assert second is None


def test_run_ingest_job_advances_phases_in_order():
    with db.connect() as conn:
        project_id, _ = db.create_project("phases", "")
        item_id = _insert_item(conn, project_id=project_id, raw_text="note")
        job_id = _insert_job(
            conn, kind="ingest", project_id=project_id, item_id=item_id
        )
        job = db.claim_next_job("worker-x")
        phases = []

        def sleep_spy(*_):
            with db.connect() as c:
                row = c.execute(
                    "SELECT phase FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                phases.append(row["phase"])

        worker.run_ingest_job(job, sleep=sleep_spy)

    assert phases == ["fetch", "analyze", "persist"]


def test_run_ingest_job_note_kind_uses_analyze_item_result(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "title": "AI Title",
            "tag": "ai-tag",
            "note": "AI note prose.",
            "swatches": [{"hex": "#112233", "label": "navy"}],
            "confidence": "high",
        }

    monkeypatch.setattr(agent, "analyze_item", fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("ai-title", "")
        raw = "a note about teal accents"
        item_id = _insert_item(conn, project_id=project_id, raw_text=raw)
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "AI Title"
        assert item["tag"] == "ai-tag"
        assert item["note_md"] == "AI note prose."
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ?", (item_id,)
        ).fetchall()
        assert [(r["hex"], r["label"]) for r in rows] == [("#112233", "navy")]
        assert calls == [
            {"title_hint": None, "url": None, "page_text": None, "user_note": raw}
        ]


def test_run_ingest_job_chains_exactly_one_resynthesize_job():
    with db.connect() as conn:
        project_id, _ = db.create_project("chain", "")
        item_id = _insert_item(conn, project_id=project_id, raw_text="note")
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        resynth = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert resynth["c"] == 1


def test_run_resynthesize_job_marks_done_and_never_touches_syntheses():
    with db.connect() as conn:
        project_id, _ = db.create_project("resynth", "")
        job_id = _insert_job(
            conn,
            kind="resynthesize",
            project_id=project_id,
            item_id=None,
            status="queued",
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status, finished_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "done"
        assert job_row["finished_at"] is not None

        synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert synth_count["c"] == 0


def test_periodic_reap_fails_stale_running_job_and_its_item():
    with db.connect() as conn:
        project_id, _ = db.create_project("reap", "")

        stale_item_id = _insert_item(
            conn, project_id=project_id, raw_text="stale", status="pending"
        )
        stale_job_id = _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=stale_item_id,
            status="running",
            heartbeat_at=_stale_iso(),
        )

        fresh_item_id = _insert_item(
            conn, project_id=project_id, raw_text="fresh", status="pending"
        )
        _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=fresh_item_id,
            status="running",
            heartbeat_at=_now_iso(),
        )

        reaped = worker.periodic_reap()
        assert reaped == 1

        stale_job = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?",
            (stale_job_id,),
        ).fetchone()
        assert stale_job["status"] == "failed"
        assert stale_job["error"] == worker.ABANDONED_ERROR

        stale_item = conn.execute(
            "SELECT status, error FROM items WHERE id = ?",
            (stale_item_id,),
        ).fetchone()
        assert stale_item["status"] == "failed"
        assert stale_item["error"] == worker.ABANDONED_ERROR

        fresh_job = conn.execute(
            "SELECT status, error FROM jobs WHERE item_id = ?",
            (fresh_item_id,),
        ).fetchone()
        assert fresh_job["status"] == "running"
        assert fresh_job["error"] is None


def test_boot_reap_fails_every_running_job_unconditionally():
    with db.connect() as conn:
        project_id, _ = db.create_project("bootreap", "")
        item_id = _insert_item(
            conn, project_id=project_id, raw_text="recent", status="pending"
        )
        job_id = _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=item_id,
            status="running",
            heartbeat_at=_now_iso(),
        )

        reaped = worker.boot_reap()
        assert reaped == 1

        job = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert job["status"] == "failed"
        assert job["error"] == worker.ABANDONED_ERROR

        item = conn.execute(
            "SELECT status, error FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        assert item["status"] == "failed"
        assert item["error"] == worker.ABANDONED_ERROR


def test_after_periodic_reap_the_next_claim_picks_up_the_queued_sibling_with_no_special_dequeue():
    with db.connect() as conn:
        project_id, _ = db.create_project("sibling", "")

        item_a = _insert_item(conn, project_id=project_id, raw_text="A")
        job_a = _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=item_a,
            status="running",
            heartbeat_at=_stale_iso(),
            created_at=_stale_iso(),
        )

        item_b = _insert_item(conn, project_id=project_id, raw_text="B")
        job_b = _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=item_b,
            status="queued",
            created_at=_now_iso(),
        )

        reaped = worker.periodic_reap()
        assert reaped == 1

        claimed = db.claim_next_job("worker-x")
        assert claimed is not None
        assert claimed["id"] == job_b
        assert claimed["status"] == "running"

        job_a_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?",
            (job_a,),
        ).fetchone()
        assert job_a_row["status"] == "failed"


def test_three_ingest_jobs_completing_in_a_row_produce_only_one_resynthesize_job():
    with db.connect() as conn:
        project_id, _ = db.create_project("three", "")
        # Create all three ingest jobs up front, before claiming any of them.
        # This keeps them older (by created_at) than any resynthesize job
        # chained mid-loop, so claim_next_job's FIFO order always hands us
        # the next ingest job rather than jumping ahead to a just-chained
        # resynthesize job.
        for i in range(3):
            db.create_note_and_ingest_job(project_id, f"note {i}")
        for i in range(3):
            job = db.claim_next_job(f"worker-{i}")
            worker.run_claimed_job(job, sleep=_noop_sleep)

        count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert count["c"] == 1


def test_resynthesize_not_chained_when_ingest_job_is_reaper_failed_instead_of_completing():
    with db.connect() as conn:
        project_id, _ = db.create_project("no-chain", "")
        item_id = _insert_item(
            conn, project_id=project_id, raw_text="orphan"
        )
        _insert_job(
            conn,
            kind="ingest",
            project_id=project_id,
            item_id=item_id,
            status="running",
            heartbeat_at=_stale_iso(),
        )

        reaped = worker.periodic_reap()
        assert reaped == 1

        count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert count["c"] == 0


def test_run_ingest_job_url_kind_extracts_title_and_thumbnail_from_og_image(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-og", "")
        base_url = local_http_server(_PageWithOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "Cool Reference"
        assert item["media_id"] is not None
        assert item["thumb_media_id"] is not None
        assert item["thumb_w"] == 640
        assert item["thumb_h"] == 448

        thumb_row = conn.execute(
            "SELECT * FROM media WHERE id = ?",
            (item["thumb_media_id"],),
        ).fetchone()
        assert thumb_row["mime"] == "image/webp"
        assert (db.MEDIA_DIR / thumb_row["path"]).exists()

        full_row = conn.execute(
            "SELECT * FROM media WHERE id = ?",
            (item["media_id"],),
        ).fetchone()
        assert full_row["mime"] == "image/jpeg"
        assert full_row["width"] == 1600
        assert full_row["height"] == 1120
        assert (db.MEDIA_DIR / full_row["path"]).exists()

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_url_kind_succeeds_without_thumbnail_when_no_og_image(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-no-og", "")
        base_url = local_http_server(_PageWithoutOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "No Image Here"
        assert item["media_id"] is None
        assert item["thumb_media_id"] is None

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_url_kind_og_image_pointing_at_private_ip_still_succeeds_without_thumbnail(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-private-og", "")
        base_url = local_http_server(_PageWithPrivateOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "Private Image Page"
        assert item["media_id"] is None
        assert item["thumb_media_id"] is None

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_url_kind_bad_og_image_bytes_skips_thumbnail_without_failing(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-bad-og", "")
        base_url = local_http_server(_PageWithBadOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "Broken Image Page"
        assert item["media_id"] is None
        assert item["thumb_media_id"] is None

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_url_kind_malformed_og_image_url_still_succeeds_without_thumbnail(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-malformed-og", "")
        base_url = local_http_server(_PageWithMalformedOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "Malformed Image Page"
        assert item["media_id"] is None
        assert item["thumb_media_id"] is None

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_url_kind_non_2xx_status_fails_with_status_code_and_no_chain(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-404", "")
        base_url = local_http_server(_NotFoundHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/missing"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert "404" in item["error"]

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_url_kind_private_ip_fails_generic_and_does_not_chain():
    with db.connect() as conn:
        project_id, _ = db.create_project("url-private-ip", "")
        source_url = "http://192.168.1.1/whatever"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert item["error"] == "could not fetch that url"
        lowered = item["error"].lower()
        for leaked in ("192.168", "private", "forbidden", "ssrf"):
            assert leaked not in lowered

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_url_kind_connection_error_fails_generic_and_does_not_chain(
    monkeypatch,
):
    s = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    unused_port = s.getsockname()[1]
    s.close()

    monkeypatch.setattr(
        net_guard,
        "resolve_public_target",
        lambda _url: net_guard.ResolvedTarget(
            ip=ipaddress.IPv4Address("127.0.0.1"),
            host="127.0.0.1",
            port=unused_port,
            scheme="http",
            path_qs="/",
        ),
    )

    with db.connect() as conn:
        project_id, _ = db.create_project("url-conn-error", "")
        source_url = "http://example.com/placeholder"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert item["error"] == "could not fetch that url"

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_fails_when_note_item_deleted_mid_flight():
    with db.connect() as conn:
        project_id, _ = db.create_project("mid-delete", "")
        raw = "a note that gets deleted"
        item_id = _insert_item(conn, project_id=project_id, raw_text=raw)
        job_id = _insert_job(
            conn, kind="ingest", project_id=project_id, item_id=item_id
        )
        job = db.claim_next_job("worker-x")

        deleted = []

        def delete_on_first_sleep(*_):
            if not deleted:
                with db.connect() as c:
                    c.execute("DELETE FROM items WHERE id = ?", (item_id,))
                    c.commit()
                deleted.append(True)

        worker.run_ingest_job(job, sleep=delete_on_first_sleep)

        job_row = conn.execute(
            "SELECT status, finished_at, error FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "failed"
        assert (
            job_row["error"]
            == f"item {item_id} was deleted before ingest completed"
        )
        assert job_row["finished_at"] is not None

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_url_kind_uses_analyze_item_result_for_title_tag_note_swatches(
    local_http_server, guard_allow_loopback, monkeypatch
):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "title": "URL AI Title",
            "tag": "url-tag",
            "note": "URL AI note.",
            "swatches": [{"hex": "#AABBCC", "label": "grey"}],
            "confidence": "medium",
        }

    monkeypatch.setattr(agent, "analyze_item", fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("url-ai", "")
        base_url = local_http_server(_PageWithoutOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "URL AI Title"
        assert item["tag"] == "url-tag"
        assert item["note_md"] == "URL AI note."
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ?", (item_id,)
        ).fetchall()
        assert [(r["hex"], r["label"]) for r in rows] == [("#AABBCC", "grey")]

        call = calls[0]
        assert call["url"] == source_url
        assert call["user_note"] is None
        assert call["title_hint"] == "No Image Here"
        assert isinstance(call["page_text"], str)
        assert call["page_text"].strip()


def test_run_ingest_job_note_kind_analyze_item_failure_fails_job_generically_no_chain(
    monkeypatch,
):
    def _raise(**kw):
        raise agent.AgentError(
            "boom: internal detail sk-fake-token-1234567890"
        )

    monkeypatch.setattr(agent, "analyze_item", _raise)
    with db.connect() as conn:
        project_id, _ = db.create_project("note-fail", "")
        raw = "a note that will break"
        item_id = _insert_item(conn, project_id=project_id, raw_text=raw)
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert item["error"] == "analysis failed"
        lowered = item["error"].lower()
        for leaked in ("boom", "internal detail", "sk-fake-token"):
            assert leaked not in lowered

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_url_kind_analyze_item_failure_fails_job_generically_no_chain(
    local_http_server, guard_allow_loopback, monkeypatch
):
    def _raise(**kw):
        raise agent.AgentError(
            "boom: internal detail sk-fake-token-1234567890"
        )

    monkeypatch.setattr(agent, "analyze_item", _raise)
    with db.connect() as conn:
        project_id, _ = db.create_project("url-fail", "")
        base_url = local_http_server(_PageWithoutOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert item["error"] == "analysis failed"
        lowered = item["error"].lower()
        for leaked in ("boom", "internal detail", "sk-fake-token"):
            assert leaked not in lowered

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_note_kind_drops_invalid_swatch_hex_but_keeps_valid_ones(
    monkeypatch,
):
    def fake(**kwargs):
        return {
            "title": "Swatch Title",
            "tag": "swatch-tag",
            "note": "note",
            "swatches": [
                {"hex": "#GGGGGG", "label": "bad"},
                {"hex": "#ABCDEF", "label": "good"},
            ],
            "confidence": "low",
        }

    monkeypatch.setattr(agent, "analyze_item", fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("swatch-filter", "")
        item_id = _insert_item(conn, project_id=project_id, raw_text="x")
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ? ORDER BY position",
            (item_id,),
        ).fetchall()
        assert [(r["hex"], r["label"]) for r in rows] == [("#ABCDEF", "good")]


def test_parse_page_extracts_body_text_stripped_of_script_and_style_content():
    html = (
        b"<html><head><title>T</title>"
        b"<style>body{color:red}</style>"
        b"<script>var x=1;</script></head>"
        b"<body><p>Real visible text here</p></body></html>"
    )
    title, image_url, body_text = worker._parse_page(html)
    assert title == "T"
    assert "color:red" not in body_text
    assert "var x=1" not in body_text
    assert "Real" in body_text
    assert "visible" in body_text


def test_parse_page_caps_body_text_at_max_page_text_chars():
    body = b"<html><body><p>" + (b"word " * 5000) + b"</p></body></html>"
    title, image_url, body_text = worker._parse_page(body)
    assert len(body_text) <= worker.MAX_PAGE_TEXT_CHARS


def test_main_exits_nonzero_without_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        worker.main()
    assert exc_info.value.code != 0
