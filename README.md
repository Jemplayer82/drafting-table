# drafting table

A self-hosted design mood board. Drop a URL, a screenshot, or a note; a headless Claude agent analyzes it, adds a reference card, and rewrites the project's "Ideas & direction" and "Open questions" from the whole board — not just an append.

Replaces a hand-maintained claude.ai Artifact that needed a chat round-trip for every update.

## ✨ Features

- Pinterest-board-style landing page — one tile per project, click through to its full board
- Drop a link, an image, or a note in one composer — no mode picker
- Server-side agent pipeline: fetch → vision analysis → whole-project re-synthesis
- Versioned synthesis history — every re-synthesis is a new version, never overwritten, with a diff view stamped to the item that triggered it
- Decisions log that survives re-synthesis — the agent can propose, never edit or delete
- No headless browser — this box sits on a LAN with unauthenticated admin surfaces; see the security notes below for why that's a hard design constraint, not an oversight

## 🚀 Quick Start

```bash
uv sync
cp .env.example .env
uv run python -m hashpw                 # generates ADMIN_PASSWORD_HASH — paste it into .env
claude setup-token                       # subscription auth, NOT an API key — see .env.example
uv run flask --app app run --debug       # web
uv run python -m worker                  # job worker, separate process
```

Visit `http://localhost:5000` (or whatever port Flask picks in debug mode).

## ⚙️ Configuration

All configuration is environment variables — see [`.env.example`](.env.example) for the full list
and generation commands. Nothing has a working default; the app refuses to start without
`ADMIN_USER` and `ADMIN_PASSWORD_HASH` set.

> **Not an API key.** `CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token` and bills against
> your Claude subscription, not per-token API usage. Don't confuse it with `ANTHROPIC_API_KEY`.

> **Secrets in production:** with an unauthenticated Portainer API on the deploy host's LAN, stack
> environment variables are readable by anyone who can reach it. Mount `ADMIN_PASSWORD_HASH`,
> `SESSION_SECRET`, and `CLAUDE_CODE_OAUTH_TOKEN` as files instead of plain compose env vars —
> the app reads a `<VAR>_FILE` path first if one's set.

## 📦 Deploy

Pre-built image only — the compose file references `ghcr.io/jemplayer82/drafting-table:latest`,
never `build: .`. CI builds and pushes on every push to `main`, gated on a gitleaks secret scan
and the test suite.

```bash
docker compose up -d
```

Two services from one image: `web` (gunicorn, serves pages and the drop endpoint) and `worker`
(a separate process that runs the actual agent pipeline). Job state lives in SQLite (WAL mode);
uploaded images and thumbnails live on a separate volume so a media flood can't take the database
down with it.

> **No headless browser, on purpose.** The deploy host sits on a LAN with an unauthenticated
> Portainer API and other admin surfaces with no auth. A browser rendering attacker-controlled
> pages would do its own DNS, follow its own redirects, and load its own subresources — none of
> which pass through any guard this app writes. URLs are fetched with a pinned HTTP client instead
> (`net_guard.py`), and thumbnails come from `og:image`/`twitter:image` meta tags.

## 📄 License

Apache License 2.0 — see [LICENSE](LICENSE).
