"""Separate-process async job worker for the drafting-table Flask app.

Background jobs run in this standalone OS process rather than inside the
gunicorn worker process (or in background threads) because in-process locks
do not span multiple gunicorn workers, and gunicorn's own worker recycling /
graceful shutdown kills in-flight background threads with no clean recovery
path.
"""

from __future__ import annotations

import datetime
import secrets
import sys
import time
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from PIL import Image

import db
import net_guard

POLL_INTERVAL_SECONDS = 1.0
REAP_INTERVAL_SECONDS = 60
HEARTBEAT_STALE_SECONDS = 600  # 10 minutes
PHASE_SLEEP_SECONDS = 0.75
ABANDONED_ERROR = "abandoned -- no heartbeat, worker likely crashed"


def _stub_title(item) -> str:
    """Derive a real title from an item's raw text or source URL."""
    text = (item["raw_text"] or item["source_url"] or "").strip() if item else ""
    if not text:
        return "Untitled note"
    first_line = text.splitlines()[0].strip()
    if not first_line:
        return "Untitled note"
    return first_line[:80] + ("..." if len(first_line) > 80 else "")


def _parse_page(html_body: bytes) -> tuple[str | None, str | None]:
    """Parses html_body ONCE and returns (title, image_url). title is the first
    <title> tag's stripped text, or None if missing/empty. image_url is the
    'content' of <meta property="og:image">, falling back to
    <meta name="twitter:image">, or None if neither is present/non-empty. image_url
    may be relative -- the caller resolves it against the page's final url."""
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
    return title, (image_url or None)


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
    absolute_url = urljoin(base_url, image_url)
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
    title, _image_url = _parse_page(result.body)
    title = title or _stub_title(item)
    media_fields = _try_fetch_thumbnail(result.body, result.final_url)
    sleep(PHASE_SLEEP_SECONDS)

    db.set_job_phase(job["id"], "persist")
    sleep(PHASE_SLEEP_SECONDS)

    db.complete_ingest_job(job["id"], job["item_id"], title, **media_fields)
    db.chain_resynthesize_job(job["project_id"])


def run_ingest_job(job, *, sleep=time.sleep) -> None:
    """Ingest handler for an already-claimed job row/dict. 'note' items still run
    the stub phase-sleep pipeline (Phase 6 will replace this with a real claude -p
    call); 'url' items run the real fetch/analyze pipeline via net_guard +
    BeautifulSoup + Pillow, with no AI involved yet."""
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
        sleep(PHASE_SLEEP_SECONDS)
    title = _stub_title(item)
    db.complete_ingest_job(job["id"], job["item_id"], title)
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
