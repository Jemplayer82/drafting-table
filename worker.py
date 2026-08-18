"""Separate-process async job worker for the drafting-table Flask app.

Background jobs run in this standalone OS process rather than inside the
gunicorn worker process (or in background threads) because in-process locks
do not span multiple gunicorn workers, and gunicorn's own worker recycling /
graceful shutdown kills in-flight background threads with no clean recovery
path.
"""

from __future__ import annotations

import datetime
import os
import re
import secrets
import sys
import time
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from PIL import Image

import agent
import db
import net_guard

POLL_INTERVAL_SECONDS = 1.0
REAP_INTERVAL_SECONDS = 60
HEARTBEAT_STALE_SECONDS = 600  # 10 minutes
PHASE_SLEEP_SECONDS = 0.75
MAX_PAGE_TEXT_CHARS = 8000
ABANDONED_ERROR = "abandoned -- no heartbeat, worker likely crashed"


def _load_secret_from_file_or_env(var: str) -> None:
    """Support `<VAR>_FILE=/path` pointing at a mounted secret file, mirroring
    app.py's own helper of the same name (duplicated, not imported, to avoid
    pulling in app.py's Flask-construction side effects in this separate
    process). Falls back to `<VAR>` as-is for local dev."""
    file_path = os.environ.get(f"{var}_FILE")
    if file_path:
        os.environ[var] = open(file_path, encoding="utf-8").read().strip()


def _parse_page(html_body: bytes) -> tuple[str | None, str | None, str]:
    """Parses html_body ONCE and returns (title, image_url, body_text).

    title is the first <title> tag's stripped text, or None if missing/empty.
    image_url is the 'content' of <meta property="og:image">, falling back to
    <meta name="twitter:image">, or None if neither is present/non-empty.
    image_url may be relative -- the caller resolves it against the page's final
    url.

    body_text is the page's visible text with <script>, <style>, and <noscript>
    removed, collapsed to a single space-separated string, then capped at
    MAX_PAGE_TEXT_CHARS to control AI cost and prompt-injection surface.
    """
    soup = BeautifulSoup(html_body, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    title = title or None

    image_url = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        image_url = og["content"].strip()
    if not image_url:
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            image_url = tw["content"].strip()

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body_text = soup.get_text(separator=" ", strip=True)[:MAX_PAGE_TEXT_CHARS]
    return title, (image_url or None), body_text


def _write_media_file(img, pillow_format: str, mime: str, ext: str, **save_kwargs) -> str:
    """Writes img to disk under db.MEDIA_DIR with a fresh secrets.token_hex(16) id
    and records it via db.insert_media. Mirrors seed.py's own id/path/resize/
    quality convention (read seed.py first for the exact numbers already
    established in Phase 2) for consistency, without importing seed's private
    helper directly -- this call site has no already-open transaction connection to
    reuse the way seed.py's does, so it goes through db.insert_media's own
    single-statement write instead. Returns the new media_id."""
    media_id = secrets.token_hex(16)
    buf = BytesIO()
    img.save(buf, format=pillow_format, **save_kwargs)
    data = buf.getvalue()
    rel_path = f"{media_id[:2]}/{media_id}.{ext}"
    abs_path = db.MEDIA_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)
    db.insert_media(media_id, rel_path, mime, img.width, img.height, len(data))
    return media_id


def _store_thumbnail_pair(img) -> tuple[str, str, int, int]:
    """Same resize/format/quality convention as seed.py's existing image storage:
    full image capped at 1600px long edge as JPEG q90, thumbnail capped at 640px
    long edge as WebP q82 (verify these exact numbers against seed.py and adjust
    here to match if seed.py's Phase 2 convention differs). Returns (media_id,
    thumb_media_id, thumb_width, thumb_height)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    full_img = img.copy()
    full_img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    media_id = _write_media_file(full_img, "JPEG", "image/jpeg", "jpg", quality=90)

    thumb_img = img.copy()
    thumb_img.thumbnail((640, 640), Image.Resampling.LANCZOS)
    thumb_id = _write_media_file(thumb_img, "WEBP", "image/webp", "webp", quality=82)

    return media_id, thumb_id, thumb_img.width, thumb_img.height


def _try_fetch_thumbnail(html_body: bytes, base_url: str) -> dict:
    """Best-effort: looks for an og:image/twitter:image meta tag, fetches it
    through the SAME SSRF-guarded net_guard.fetch used for the page itself (an
    og:image URL is just as untrusted as the page url), decodes it with Pillow,
    and stores it. Returns {} (no thumbnail) on ANY failure at any stage --
    missing tag, guard rejection, non-2xx, or bytes that don't decode as an image
    -- since a missing or bad thumbnail is a normal, expected outcome and must
    NEVER fail the calling ingest job. Returns
    {'media_id','thumb_media_id','thumb_w','thumb_h'} on success."""
    image_url = _parse_page(html_body)[1]
    if not image_url:
        return {}
    try:
        absolute_url = urljoin(base_url, image_url)
    except ValueError:
        return {}
    try:
        img_result = net_guard.fetch(absolute_url)
    except net_guard.NetGuardError:
        return {}
    if not (200 <= img_result.status_code < 300):
        return {}
    try:
        img = Image.open(BytesIO(img_result.body))
        img.load()
        media_id, thumb_id, thumb_w, thumb_h = _store_thumbnail_pair(img)
    except Exception:
        # Deliberately broad: this is decoding/processing arbitrary bytes fetched
        # from an untrusted remote url, which can fail in many Pillow-specific
        # ways (UnidentifiedImageError, OSError, DecompressionBombError, ...). Per
        # spec, ANY failure here degrades to "no thumbnail", never fails the item.
        return {}
    return {
        "media_id": media_id,
        "thumb_media_id": thumb_id,
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
    }


_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")  # matches app.py's own _HEX_RE exactly


def _run_analysis_or_fail(job, *, title_hint, url, page_text, user_note) -> dict | None:
    """Calls agent.analyze_item with the given inputs.

    On ANY agent.AgentError (timeout, nonzero exit, schema validation failure,
    subprocess error), logs a short context line to stderr, fails the job via
    db.fail_job with a SHORT GENERIC message -- never the raw (even if already
    scrubbed) CLI-derived text -- and returns None. The caller MUST check for None
    and return immediately, WITHOUT chaining a resynthesize job, matching this
    file's existing failure-path convention for fetch failures.

    On success returns the validated analysis dict unchanged.
    """
    try:
        return agent.analyze_item(
            title_hint=title_hint, url=url, page_text=page_text, user_note=user_note
        )
    except agent.AgentError as exc:
        print(f"ingest job {job['id']} analysis failed: {exc}", file=sys.stderr)
        db.fail_job(job["id"], job["item_id"], "analysis failed")
        return None


def _run_url_ingest(job, item, *, sleep) -> None:
    db.set_job_phase(job["id"], "fetch")
    try:
        result = net_guard.fetch(item["source_url"])
    except net_guard.NetGuardError as exc:
        print(f"ingest job {job['id']} fetch failed: {exc}", file=sys.stderr)
        db.fail_job(job["id"], job["item_id"], "could not fetch that url")
        return
    if not (200 <= result.status_code < 300):
        db.fail_job(
            job["id"], job["item_id"], f"could not fetch that url (http {result.status_code})"
        )
        return

    db.set_job_phase(job["id"], "analyze")
    title_hint, _image_url, page_text = _parse_page(result.body)
    analysis = _run_analysis_or_fail(
        job,
        title_hint=title_hint,
        url=item["source_url"],
        page_text=page_text,
        user_note=None,
    )
    if analysis is None:
        return
    media_fields = _try_fetch_thumbnail(result.body, result.final_url)
    sleep(PHASE_SLEEP_SECONDS)

    db.set_job_phase(job["id"], "persist")
    sleep(PHASE_SLEEP_SECONDS)

    item = db.get_item(job["item_id"])
    if item is None:
        db.fail_job(
            job["id"], job["item_id"], f"item {job['item_id']} was deleted before ingest completed"
        )
        return

    swatches = [sw for sw in analysis["swatches"] if _HEX_RE.fullmatch(sw["hex"])]
    db.complete_ingest_job(
        job["id"],
        job["item_id"],
        analysis["title"],
        tag=analysis["tag"],
        note_md=analysis["note"],
        **media_fields,
    )
    db.replace_swatches(job["item_id"], swatches)
    db.chain_resynthesize_job(job["project_id"])


def run_ingest_job(job, *, sleep=time.sleep) -> None:
    """Ingest handler for an already-claimed job row/dict.

    Both 'note' and 'url' items run a real agent.analyze_item() call during the
    'analyze' phase. If agent.analyze_item() raises any agent.AgentError, the job
    is failed with the generic message 'analysis failed', no CLI-derived details
    are surfaced, and no resynthesize job is chained.

    'url' items additionally fetch the page via net_guard, extract visible body
    text and an optional og:image thumbnail, and pass those into the analysis.
    """
    item = db.get_item(job["item_id"])
    if item is None:
        db.fail_job(
            job["id"], job["item_id"], f"item {job['item_id']} was deleted before ingest completed"
        )
        return

    if item["kind"] == "url":
        _run_url_ingest(job, item, sleep=sleep)
        return

    for phase in ("fetch", "analyze", "persist"):
        db.set_job_phase(job["id"], phase)
        if phase == "analyze":
            analysis = _run_analysis_or_fail(
                job,
                title_hint=None,
                url=None,
                page_text=None,
                user_note=item["raw_text"],
            )
            if analysis is None:
                return
        sleep(PHASE_SLEEP_SECONDS)

    item = db.get_item(job["item_id"])
    if item is None:
        db.fail_job(
            job["id"], job["item_id"], f"item {job['item_id']} was deleted before ingest completed"
        )
        return

    swatches = [sw for sw in analysis["swatches"] if _HEX_RE.fullmatch(sw["hex"])]
    db.complete_ingest_job(
        job["id"],
        job["item_id"],
        analysis["title"],
        tag=analysis["tag"],
        note_md=analysis["note"],
    )
    db.replace_swatches(job["item_id"], swatches)
    db.chain_resynthesize_job(job["project_id"])


def run_resynthesize_job(job, *, sleep=time.sleep) -> None:
    """STUB resynthesize handler."""
    sleep(PHASE_SLEEP_SECONDS)
    db.complete_job(job["id"])


def run_claimed_job(job, *, sleep=time.sleep) -> None:
    """Dispatch a claimed job to its kind-specific handler."""
    kind = job["kind"]
    if kind == "ingest":
        run_ingest_job(job, sleep=sleep)
    elif kind == "resynthesize":
        run_resynthesize_job(job, sleep=sleep)
    else:
        raise ValueError(f"unknown job kind: {job['kind']!r}")


def periodic_reap() -> int:
    """Fail running jobs whose heartbeat is older than the stale cutoff.

    This sweep is run by main()'s own loop on a timer, not a separate
    process or thread.
    """
    cutoff = (
        datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    ).isoformat()
    stale = db.find_stale_running_jobs(cutoff)
    for row in stale:
        db.fail_job(row["id"], row["item_id"], ABANDONED_ERROR)
    return len(stale)


def boot_reap() -> int:
    """Fail every running job unconditionally at process boot.

    This single-worker-process architecture (one worker container per
    docker-compose) means the only thing that ever sets ``status='running'``
    anywhere in this codebase is this same process's own ``claim_next_job``
    call from its own main loop; so any job still ``'running'`` at process
    boot must be a leftover from a killed PRIOR instance of this same process
    (docker restart, SIGKILL, etc.), never a currently-live sibling -- there
    is no valid scenario where a running job survives this process's own
    restart.
    """
    running = db.find_all_running_jobs()
    for row in running:
        db.fail_job(row["id"], row["item_id"], ABANDONED_ERROR)
    return len(running)


def main() -> None:
    _load_secret_from_file_or_env("CLAUDE_CODE_OAUTH_TOKEN")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print(
            "CLAUDE_CODE_OAUTH_TOKEN must be set (or CLAUDE_CODE_OAUTH_TOKEN_FILE pointing "
            "at a mounted secret file) -- this worker refuses to start without it, since "
            "every ingest job's analyze phase now runs a real claude -p call. Generate one "
            "with:\n\n    claude setup-token\n",
            file=sys.stderr,
        )
        sys.exit(1)

    worker_id = secrets.token_hex(8)
    db.init_db()
    print(f"worker starting, worker_id={worker_id}", file=sys.stderr)
    reaped = boot_reap()
    if reaped:
        print(f"worker boot: reaped {reaped} orphaned running job(s)", file=sys.stderr)
    last_reap = time.monotonic()
    while True:
        job = db.claim_next_job(worker_id)
        if job is not None:
            try:
                run_claimed_job(job)
            except Exception as exc:
                db.fail_job(job["id"], job["item_id"], f"job crashed: {exc}")
                print(f"job {job['id']} failed: {exc}", file=sys.stderr)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)
        if time.monotonic() - last_reap >= REAP_INTERVAL_SECONDS:
            reaped = periodic_reap()
            if reaped:
                print(f"periodic reaper failed {reaped} stale job(s)", file=sys.stderr)
            last_reap = time.monotonic()


if __name__ == "__main__":
    main()
