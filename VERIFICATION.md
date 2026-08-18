# Manual Verification Log

This file tracks manual verification steps required by the project's phase plans that cannot be executed by the automated pipeline (no real Claude subscription credentials, no interactive browser). It exists so required-but-unexecuted gates are recorded explicitly, not silently absent.

---

## Phase 6 — Step 5: real end-to-end AI verification

**Status:** BLOCKED

**Reason:** No real `CLAUDE_CODE_OAUTH_TOKEN` is available in the automated pipeline's execution environment; the repo's `.env` only contains the placeholder value `changeme`. This step is intentionally a manual, human-run check per the plan. Steps 1–4 are already covered by the test suite using a fake `claude` binary on PATH (see `CONTRIBUTING.md`'s Standards section), so no automated process burns real Claude subscription quota or needs real credentials.

**What still needs to happen:**

a. Run the web app (`uv run flask --app app run --debug`) and the worker (`uv run python -m worker`) with a real `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) set in the environment.

b. Drop a real note through the UI composer; confirm the resulting item reaches `status=ready` with a genuinely AI-authored `title`/`tag`/`note`, and an empty `swatches` array (the analysis system prompt requires `swatches=[]` on every text-only call — see `agent.py`'s `ANALYZE_ITEM_SCHEMA` and `_ANALYZE_ITEM_SYSTEM_PROMPT`).

c. Drop a real URL through the UI composer; confirm the same `status=ready` / genuinely AI-authored `title`/`tag`/`note` outcome, and separately confirm the `og:image`/`twitter:image` thumbnail pipeline (`worker.py`'s `_try_fetch_thumbnail` / `_store_thumbnail_pair`) is unaffected by the real agent call.

d. Update this file's **Status** to `PASSED` or `FAILED` with the date and a one-line summary of what was actually observed once a human has actually run the above. Never edit the **Status** field without having actually run the check.

**Not yet run by:** automated pipeline
