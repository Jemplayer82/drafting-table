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


def _run_ingest_and_get_item(
    conn,
    project_id,
    *,
    raw_text=None,
    source_url=None,
    worker_id="worker-x",
):
    item_id = _insert_item(
        conn,
        project_id=project_id,
        raw_text=raw_text,
        source_url=source_url,
    )
    _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
    job = db.claim_next_job(worker_id)
    worker.run_ingest_job(job, sleep=_noop_sleep)
    return db.get_item(item_id)


def _make_jpeg_bytes() -> bytes:
    img = Image.new("RGB", (300, 200), color=(120, 180, 220))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


_TEST_JPEG = _make_jpeg_bytes()


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
            self.wfile.write(_TEST_JPEG)
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


def test_run_ingest_job_marks_item_ready_with_real_derived_title():
    with db.connect() as conn:
        project_id, _ = db.create_project("title", "")
        raw = "a note about teal accents"
        item_id = _insert_item(conn, project_id=project_id, raw_text=raw)
        job_id = _insert_job(
            conn, kind="ingest", project_id=project_id, item_id=item_id
        )
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"]
        assert "placeholder" not in item["title"].lower()
        assert item["title"] == raw[:80] + ("..." if len(raw) > 80 else "")

        job_row = conn.execute(
            "SELECT status, finished_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert job_row["status"] == "done"
        assert job_row["finished_at"] is not None


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


def test_run_ingest_job_derives_untitled_when_text_and_url_are_empty():
    with db.connect() as conn:
        project_id, _ = db.create_project("empty", "")
        item = _run_ingest_and_get_item(
            conn, project_id, raw_text=None, source_url=None
        )
        assert item["status"] == "ready"
        assert "placeholder" not in item["title"].lower()
        assert item["title"] == "Untitled note"


def test_run_ingest_job_derives_untitled_for_whitespace_text_even_with_source_url():
    with db.connect() as conn:
        project_id, _ = db.create_project("whitespace", "")
        item = _run_ingest_and_get_item(
            conn,
            project_id,
            raw_text="   \t\n",
            source_url="http://example.com/note",
        )
        assert item["status"] == "ready"
        assert item["title"] == "Untitled note"


def test_run_ingest_job_derives_title_from_first_line_of_multiline_raw_text():
    with db.connect() as conn:
        project_id, _ = db.create_project("multiline", "")
        item = _run_ingest_and_get_item(
            conn,
            project_id,
            raw_text="first line of the note\nsecond line\nthird line",
        )
        assert item["status"] == "ready"
        assert item["title"] == "first line of the note"


def test_run_ingest_job_uses_first_non_blank_line_when_leading_line_is_whitespace():
    with db.connect() as conn:
        project_id, _ = db.create_project("blankfirst", "")
        item = _run_ingest_and_get_item(
            conn,
            project_id,
            raw_text="   \nreal content on a later line",
        )
        assert item["status"] == "ready"
        assert item["title"] == "real content on a later line"


def test_run_ingest_job_truncates_long_first_line_to_eighty_chars_plus_ellipsis():
    with db.connect() as conn:
        project_id, _ = db.create_project("truncate", "")
        long_first = "a" * 100
        item = _run_ingest_and_get_item(
            conn,
            project_id,
            raw_text=long_first + "\nsecond line",
        )
        assert item["status"] == "ready"
        assert item["title"] == "a" * 80 + "..."
        assert len(item["title"]) == 83


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
        assert item["thumb_w"] > 0
        assert item["thumb_h"] > 0

        thumb_row = conn.execute(
            "SELECT * FROM media WHERE id = ?",
            (item["thumb_media_id"],),
        ).fetchone()
        assert thumb_row["mime"] == "image/webp"
        assert (db.MEDIA_DIR / thumb_row["path"]).exists()

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
