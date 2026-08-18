"""Tests for worker.py.

These tests exercise the separate-process job worker without importing
Flask or app.py.
"""

from __future__ import annotations

import datetime
import http.server
import importlib
import ipaddress
import json
import os
import secrets
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
    media_id=None,
):
    cur = conn.execute(
        """
        INSERT INTO items (project_id, kind, raw_text, source_url, status,
                           title, error, media_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            kind,
            raw_text,
            source_url,
            status,
            title,
            error,
            media_id,
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
    trigger_item_id=None,
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
        INSERT INTO jobs (kind, status, project_id, item_id, trigger_item_id, phase,
                          heartbeat_at, created_at, finished_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind,
            status,
            project_id,
            item_id,
            trigger_item_id,
            phase,
            heartbeat_at,
            created_at,
            None,
            error,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _insert_ready_item(
    conn,
    *,
    project_id,
    title,
    note_md,
    updated_at=None,
):
    if updated_at is None:
        updated_at = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO items (project_id, kind, status, title, note_md, position,
                           created_at, updated_at)
        VALUES (?, 'note', 'ready', ?, ?, 0, ?, ?)
        """,
        (project_id, title, note_md, _now_iso(), updated_at),
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


def _make_huge_png_bytes() -> bytes:
    img = Image.new("RGB", (7001, 5716), color=(80, 160, 240))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


class _PageWithHugeOgImageHandler(http.server.BaseHTTPRequestHandler):
    _image_bytes = None

    def do_GET(self):
        if self.path == "/page":
            body = (
                b"<html><head><title>Huge Image Page</title>"
                b'<meta property="og:image" content="/img.png"></head>'
                b"<body></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/img.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(self._image_bytes)
        else:
            self.send_response(404)
            self.end_headers()

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
    def _fake(
        *,
        title_hint=None, url=None, page_text=None, user_note=None,
        image_bytes=None, image_media_type=None,
    ):
        return {
            "title": title_hint or "Mock Title",
            "tag": "mock-tag",
            "note": "Mock analysis note.",
            "swatches": [],
            "confidence": "medium",
            "alt_text": "",
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
            "alt_text": "AI-generated note alt text.",
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
        assert item["alt_text"] == "AI-generated note alt text."
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


@pytest.mark.parametrize("count", [0, 1, 2])
def test_run_resynthesize_job_under_3_ready_items_never_calls_agent(
    count, monkeypatch
):
    def _fake(_context):
        raise AssertionError("agent.resynthesize_project should not be called")

    monkeypatch.setattr(agent, "resynthesize_project", _fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("under3", "")
        for i in range(count):
            _insert_item(
                conn, project_id=project_id, raw_text=f"note {i}", status="ready"
            )
        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "done"

        synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert synth_count["c"] == 0


def test_run_resynthesize_job_happy_path_writes_synthesis_and_proposed_decisions(
    monkeypatch,
):
    def _fake(context):
        assert context["item_count"] == 3
        return {
            "direction_md": "new direction",
            "open_questions": [{"question": "q1", "why": "w1"}],
            "proposed_decisions": [
                {"decision": "d1", "rationale": "r1"},
                {"decision": "d2", "rationale": "r2"},
            ],
        }

    monkeypatch.setattr(agent, "resynthesize_project", _fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("happy", "")
        item_ids = [
            _insert_item(
                conn, project_id=project_id, raw_text=f"note {i}", status="ready"
            )
            for i in range(3)
        ]
        job_id = _insert_job(
            conn,
            kind="resynthesize",
            project_id=project_id,
            item_id=None,
            trigger_item_id=item_ids[0],
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        synth = conn.execute(
            """
            SELECT version, direction_md, questions_json, item_count,
                   trigger_item_id, job_id
            FROM syntheses WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert synth is not None
        assert synth["version"] == 1
        assert synth["direction_md"] == "new direction"
        assert json.loads(synth["questions_json"]) == [
            {"question": "q1", "why": "w1"}
        ]
        assert synth["item_count"] == 3
        assert synth["trigger_item_id"] == item_ids[0]
        assert synth["job_id"] == job_id

        decisions = conn.execute(
            """
            SELECT body_md, rationale_md, source, status
            FROM decisions WHERE project_id = ? ORDER BY id
            """,
            (project_id,),
        ).fetchall()
        assert [
            (d["body_md"], d["rationale_md"], d["source"], d["status"])
            for d in decisions
        ] == [
            ("d1", "r1", "agent", "proposed"),
            ("d2", "r2", "agent", "proposed"),
        ]

        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "done"


def test_run_resynthesize_job_agent_failure_fails_job_generically_no_writes(
    monkeypatch,
):
    def _fake(_context):
        raise agent.AgentTimeoutError("claude CLI exceeded 180s timeout")

    monkeypatch.setattr(agent, "resynthesize_project", _fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("agent-fail", "")
        for i in range(3):
            _insert_item(
                conn, project_id=project_id, raw_text=f"note {i}", status="ready"
            )
        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "failed"
        assert job_row["error"] == "resynthesis failed"

        synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert synth_count["c"] == 0

        decision_count = conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert decision_count["c"] == 0


def test_run_resynthesize_job_append_only_decisions_guarantee_end_to_end(fake_claude):
    fake_claude(
        structured_output={
            "direction_md": "New direction.",
            "open_questions": [],
            "proposed_decisions": [
                {"decision": "Use teal", "rationale": "converged"},
                {"decision": "Drop the grid", "rationale": "converged"},
            ],
        }
    )
    with db.connect() as conn:
        project_id, _ = db.create_project("append-only", "")
        for title in ("Alpha", "Beta", "Gamma"):
            _insert_ready_item(
                conn, project_id=project_id, title=title, note_md=f"note {title}"
            )
        db.insert_decision(project_id, "First accepted", "because one")
        db.insert_decision(project_id, "Second accepted", "because two")

        pre_decisions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM decisions WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]

        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "done"

        for pre in pre_decisions:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (pre["id"],)
            ).fetchone()
            assert dict(row) == pre

        count = conn.execute(
            "SELECT COUNT(*) AS c FROM decisions WHERE project_id = ?",
            (project_id,),
        ).fetchone()["c"]
        assert count == 4

        new_rows = conn.execute(
            """
            SELECT * FROM decisions
            WHERE project_id = ? AND source = 'agent' AND status = 'proposed'
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
        assert len(new_rows) == 2
        assert sorted(r["body_md"] for r in new_rows) == ["Drop the grid", "Use teal"]
        assert all(
            r["source"] == "agent" and r["status"] == "proposed" for r in new_rows
        )


def test_run_resynthesize_job_increments_version_across_two_real_runs(fake_claude):
    with db.connect() as conn:
        project_id, _ = db.create_project("versions", "")
        for i in range(3):
            _insert_ready_item(
                conn, project_id=project_id, title=f"Item {i}", note_md="n"
            )

        _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        fake_claude(
            structured_output={
                "direction_md": "First direction.",
                "open_questions": [],
                "proposed_decisions": [],
            }
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        assert db.latest_synthesis_version(project_id) == 1

        db.chain_resynthesize_job(project_id)
        fake_claude(
            structured_output={
                "direction_md": "Second direction.",
                "open_questions": [],
                "proposed_decisions": [],
            }
        )
        job2 = db.claim_next_job("worker-y")
        worker.run_resynthesize_job(job2, sleep=_noop_sleep)

        assert db.latest_synthesis_version(project_id) == 2

        rows = conn.execute(
            """
            SELECT version, direction_md
            FROM syntheses WHERE project_id = ? ORDER BY version
            """,
            (project_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["version"] == 1
        assert rows[0]["direction_md"] == "First direction."
        assert rows[1]["version"] == 2
        assert rows[1]["direction_md"] == "Second direction."


@pytest.mark.parametrize("count", [0, 1, 2])
def test_run_resynthesize_job_under_three_ready_items_never_invokes_fake_claude_binary(
    count, fake_claude, tmp_path
):
    fake_claude(
        structured_output={
            "direction_md": "Should not run.",
            "open_questions": [],
            "proposed_decisions": [{"decision": "d", "rationale": "r"}],
        }
    )
    with db.connect() as conn:
        project_id, _ = db.create_project(f"under3-real-{count}", "")
        for i in range(count):
            _insert_ready_item(
                conn, project_id=project_id, title=f"Ready {i}", note_md="n"
            )
        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "done"

        synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()["c"]
        assert synth_count == 0

    dump_path = tmp_path / "fake_claude_dump.json"
    assert not dump_path.exists()


def test_run_resynthesize_job_failure_path_leaves_tables_untouched(fake_claude):
    fake_claude(exit_code=1, stderr="boom " + "A" * 30)
    with db.connect() as conn:
        project_id, _ = db.create_project("resynth-fail-real", "")
        for i in range(3):
            _insert_ready_item(
                conn, project_id=project_id, title=f"Item {i}", note_md="n"
            )
        db.insert_decision(project_id, "Existing decision", "rationale")

        pre_decisions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM decisions WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]
        pre_synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()["c"]

        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "failed"
        assert job_row["error"] == "resynthesis failed"
        assert "boom" not in job_row["error"].lower()

        synth_count = conn.execute(
            "SELECT COUNT(*) AS c FROM syntheses WHERE project_id = ?",
            (project_id,),
        ).fetchone()["c"]
        assert synth_count == pre_synth_count == 0

        post_decisions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM decisions WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]
        assert post_decisions == pre_decisions


def test_run_resynthesize_job_digest_caps_at_25_of_30_ready_items(fake_claude, tmp_path):
    fake_claude(
        structured_output={
            "direction_md": "ok",
            "open_questions": [],
            "proposed_decisions": [],
        }
    )
    with db.connect() as conn:
        project_id, _ = db.create_project("cap-30", "")
        base = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        for i in range(30):
            _insert_ready_item(
                conn,
                project_id=project_id,
                title=f"Item {i}",
                note_md="n",
                updated_at=(base + datetime.timedelta(seconds=i)).isoformat(),
            )
        job_id = _insert_job(
            conn, kind="resynthesize", project_id=project_id, item_id=None
        )
        job = db.claim_next_job("worker-x")
        worker.run_resynthesize_job(job, sleep=_noop_sleep)

        job_row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert job_row["status"] == "done"

    dump_path = tmp_path / "fake_claude_dump.json"
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    prompt = data["argv"][-1]
    for i in range(5):
        # trailing "\n" disambiguates e.g. "Item 1" from "Item 15"/"Item 19"
        assert f"Title: Item {i}\n" not in prompt
    assert prompt.count("Title: Item ") == 25


def test_run_ingest_job_url_kind_passes_trigger_item_id_when_chaining(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("chain-trigger-url", "")
        base_url = local_http_server(_PageWithoutOgImageHandler)
        guard_allow_loopback(urlsplit(base_url).port)
        source_url = base_url + "/page"
        url_item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="url",
            source_url=source_url,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=url_item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        resynth = conn.execute(
            """
            SELECT trigger_item_id, item_id
            FROM jobs WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert resynth is not None
        assert resynth["trigger_item_id"] == url_item_id
        assert resynth["item_id"] is None


def test_run_ingest_job_note_kind_passes_trigger_item_id_when_chaining():
    with db.connect() as conn:
        project_id, _ = db.create_project("chain-trigger-note", "")
        note_item_id = _insert_item(
            conn, project_id=project_id, raw_text="a note"
        )
        _insert_job(
            conn, kind="ingest", project_id=project_id, item_id=note_item_id
        )
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        resynth = conn.execute(
            """
            SELECT trigger_item_id, item_id
            FROM jobs WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert resynth is not None
        assert resynth["trigger_item_id"] == note_item_id
        assert resynth["item_id"] is None


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


def test_run_ingest_job_url_kind_huge_og_image_declared_pixels_skips_thumbnail_without_failing(
    local_http_server, guard_allow_loopback
):
    with db.connect() as conn:
        project_id, _ = db.create_project("url-huge-og", "")
        _PageWithHugeOgImageHandler._image_bytes = _make_huge_png_bytes()
        base_url = local_http_server(_PageWithHugeOgImageHandler)
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
        assert item["title"] == "Huge Image Page"
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


def test_run_ingest_job_image_kind_happy_path(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "title": "Image Title",
            "tag": "image-tag",
            "note": "Image note.",
            "swatches": [{"hex": "#112233", "label": "dark"}],
            "confidence": "medium",
            "alt_text": "Image alt text.",
        }

    monkeypatch.setattr(agent, "analyze_item", fake)
    with db.connect() as conn:
        project_id, _ = db.create_project("image-happy", "")
        media_id = secrets.token_hex(16)
        rel_path = f"{media_id[:2]}/{media_id}.jpg"
        full_path = db.MEDIA_DIR / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_TEST_JPEG)
        # Deliberately NOT "image/jpeg" here: worker.py forwarding a hardcoded
        # "image/jpeg" regardless of the stored row's real mime would slip
        # past every other image-kind test in this file, since they all
        # happen to use "image/jpeg" for both the stored value and the
        # asserted value. Using a distinct mime (image/webp is one of the
        # app's own supported upload formats) proves the real column value is
        # what's actually threaded through to agent.analyze_item.
        db.insert_media(media_id, rel_path, "image/webp", 300, 200, len(_TEST_JPEG))

        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="image",
            media_id=media_id,
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "ready"
        assert item["title"] == "Image Title"
        assert item["tag"] == "image-tag"
        assert item["note_md"] == "Image note."
        assert item["alt_text"] == "Image alt text."
        assert item["media_id"] == media_id
        assert item["thumb_media_id"] is not None
        rows = conn.execute(
            "SELECT hex, label FROM swatches WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        assert [(r["hex"], r["label"]) for r in rows] == [("#112233", "dark")]
        assert calls == [
            {
                "title_hint": None,
                "url": None,
                "page_text": None,
                "user_note": None,
                "image_bytes": _TEST_JPEG,
                "image_media_type": "image/webp",
            }
        ]

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 1


def test_run_ingest_job_image_kind_missing_media_fails_generic_no_chain():
    with db.connect() as conn:
        project_id, _ = db.create_project("image-missing", "")
        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="image",
            media_id="nonexistent-media-id",
            status="pending",
        )
        _insert_job(conn, kind="ingest", project_id=project_id, item_id=item_id)
        job = db.claim_next_job("worker-x")
        worker.run_ingest_job(job, sleep=_noop_sleep)

        item = db.get_item(item_id)
        assert item["status"] == "failed"
        assert item["error"] == "stored image was missing"

        chained = conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE kind = 'resynthesize' AND project_id = ?
            """,
            (project_id,),
        ).fetchone()
        assert chained["c"] == 0


def test_run_ingest_job_image_kind_analyze_item_failure_fails_job_generically_no_chain(
    monkeypatch,
):
    def _raise(**kw):
        raise agent.AgentError("boom: internal detail sk-fake-token-1234567890")

    monkeypatch.setattr(agent, "analyze_item", _raise)
    with db.connect() as conn:
        project_id, _ = db.create_project("image-fail", "")
        media_id = secrets.token_hex(16)
        rel_path = f"{media_id[:2]}/{media_id}.jpg"
        full_path = db.MEDIA_DIR / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_TEST_JPEG)
        db.insert_media(media_id, rel_path, "image/jpeg", 300, 200, len(_TEST_JPEG))

        item_id = _insert_item(
            conn,
            project_id=project_id,
            kind="image",
            media_id=media_id,
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
            "alt_text": "URL AI alt text.",
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
        assert item["alt_text"] == "URL AI alt text."
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


def test_load_secret_from_file_or_env_reads_file_and_strips_whitespace(
    tmp_path, monkeypatch
):
    secret_file = tmp_path / "oauth-token.txt"
    # Real mounted secret files typically end with a trailing newline; include
    # leading/trailing whitespace to prove .strip() is actually applied.
    secret_file.write_text(
        "  mounted-oauth-token-from-file\n", encoding="utf-8"
    )

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_FILE", str(secret_file))

    worker._load_secret_from_file_or_env("CLAUDE_CODE_OAUTH_TOKEN")

    assert (
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        == "mounted-oauth-token-from-file"
    )
