# Manual Verification Log

This file tracks manual verification steps required by the project's phase plans that cannot be executed by the automated pipeline (no real Claude subscription credentials, no interactive browser). It exists so required-but-unexecuted gates are recorded explicitly, not silently absent.

---

## Phase 7 — Step 7: real end-to-end re-synthesis verification

**Status:** PASSED (2026-08-18)

**Observed:** Ran web+worker with a real `CLAUDE_CODE_OAUTH_TOKEN` against the
project seeded from the real artifact export (`Studio Portfolio Site`, which
starts with 2 ready items, 2 user-accepted decisions, and a seed-imported v1
synthesis). Dropped a real URL (`https://linear.app`) through the API to
cross the 3-item resynthesis threshold. The ingest job produced a genuinely
AI-authored item (title "Linear's numbered-stage product tour (1.0 Intake →
5.0 Monitor)", tag `pipeline-narrative`, a specific multi-sentence note
correctly identifying the actual page structure) with `swatches=[]` as
expected for a text-only call. The chained resynthesize job then produced a
`syntheses` row at `version=2` (up from seed-imported `version=1`) with
`item_count=3` and `trigger_item_id` set to the new item's id. `direction_md`
was completely rewritten from scratch — not edited or extended — explicitly
naming and reasoning about all 3 references (including the two pre-existing
ones), correctly identifying and naming the real tension between the
project's "editorial, not corporate" framing and the newly-dropped SaaS-tour
reference, and explicitly citing both settled decisions by content ("the
settled fixed-left-rail nav", "the settled serif-display + grotesk-body
pairing") rather than re-litigating them. `open_questions` was fully
regenerated with 5 new specific (non-generic) questions. The two original
`source='user', status='accepted'` decision rows were confirmed byte-for-byte
unchanged in the database, and exactly one new `source='agent',
status='proposed'` decision appeared from `proposed_decisions` — the
append-only guarantee held under a real (not fake-CLI) run. (One false alarm
during review: the old seed-imported decision text appeared to contain a
corrupted `�` character when printed through a Python console session;
`repr()` on the raw stored value confirmed the actual stored codepoint is
U+2014 (a real em-dash) — a Windows console print-encoding artifact, not a
data bug. No code changes were needed as a result of this check.)

**Not yet run by:** automated pipeline (same reason as Phase 6 below — no
real credentials in that environment).

---

## Phase 6 — Step 5: real end-to-end AI verification

**Status:** PASSED (2026-08-17)

**Observed:** Ran web+worker with a real `CLAUDE_CODE_OAUTH_TOKEN`. Dropped a plain
text note describing a design pattern -- item reached `status=ready` with a
specific, non-generic AI-authored title ("Portfolio in Oversized Type, Accent
Only on Hover"), tag ("restrained-hover-accent"), and a 3-sentence note
correctly identifying the named mechanism (color as an interactivity signal,
withheld until hover) rather than vague adjectives -- `swatches=[]` as
required for a text-only call. Dropped `https://stripe.com` -- item reached
`status=ready` with an AI-authored title/tag/note that correctly described
the page's actual real content structure (a repeating "three-stat" proof
pattern across customer/audience sections), and `thumb_media_id` was
populated -- confirming the Phase 5 `og:image` thumbnail pipeline is
unaffected by the new analysis step. Both calls used real Claude subscription
budget as expected; no code changes were needed as a result of this check.

<details>
<summary>Original BLOCKED entry (superseded above)</summary>

**Status:** BLOCKED

**Reason:** No real `CLAUDE_CODE_OAUTH_TOKEN` is available in the automated pipeline's execution environment; the repo's `.env` only contains the placeholder value `changeme`. This step is intentionally a manual, human-run check per the plan. Steps 1–4 are already covered by the test suite using a fake `claude` binary on PATH (see `CONTRIBUTING.md`'s Standards section), so no automated process burns real Claude subscription quota or needs real credentials.

**What still needs to happen:**

a. Run the web app (`uv run flask --app app run --debug`) and the worker (`uv run python -m worker`) with a real `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) set in the environment.

b. Drop a real note through the UI composer; confirm the resulting item reaches `status=ready` with a genuinely AI-authored `title`/`tag`/`note`, and an empty `swatches` array (the analysis system prompt requires `swatches=[]` on every text-only call — see `agent.py`'s `ANALYZE_ITEM_SCHEMA` and `_ANALYZE_ITEM_SYSTEM_PROMPT`).

c. Drop a real URL through the UI composer; confirm the same `status=ready` / genuinely AI-authored `title`/`tag`/`note` outcome, and separately confirm the `og:image`/`twitter:image` thumbnail pipeline (`worker.py`'s `_try_fetch_thumbnail` / `_store_thumbnail_pair`) is unaffected by the real agent call.

d. Update this file's **Status** to `PASSED` or `FAILED` with the date and a one-line summary of what was actually observed once a human has actually run the above. Never edit the **Status** field without having actually run the check.

**Not yet run by:** automated pipeline

</details>
