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

import db

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


def run_ingest_job(job, *, sleep=time.sleep) -> None:
    """STUB ingest handler for an already-claimed job row/dict."""
    for phase in ("fetch", "analyze", "persist"):
        db.set_job_phase(job["id"], phase)
        sleep(PHASE_SLEEP_SECONDS)
    item = db.get_item(job["item_id"])
    if item is None:
        db.fail_job(
            job["id"], job["item_id"], f"item {job['item_id']} was deleted before ingest completed"
        )
        return
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
