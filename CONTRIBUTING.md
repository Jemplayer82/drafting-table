# Contributing

Personal-infra project, but the standards still apply.

## Setup

```bash
uv sync
cp .env.example .env   # fill in ADMIN_USER/ADMIN_PASSWORD_HASH, SESSION_SECRET, CLAUDE_CODE_OAUTH_TOKEN
uv run python -m hashpw          # generates ADMIN_PASSWORD_HASH
uv run flask --app app run --debug
uv run python -m worker          # separate process — the job worker doesn't run inside the web process
```

## Standards

- **Branches:** `feature/<slug>`, `fix/<slug>`.
- **Commits:** Conventional-ish, imperative mood.
- **Secrets never get committed.** Real values live only in the gitignored `.env`. In production, the
  admin password hash and the Claude OAuth token are mounted as files, not passed as plain compose
  env vars — see `.env.example` for why. The CI gitleaks gate and the local `secret-scan-guard.js`
  hook are backstops, not an excuse to be sloppy.
- **Tests:** `uv run pytest` must be green before any push to `main`. Agent tests run against a fake
  `claude` binary on `PATH` — no network calls, no quota burn.
- **SSRF guard:** any change to `net_guard.py` needs a matching bypass-class test (CGNAT, NAT64,
  `::`-mapped v4, redirect-to-private, non-80/443 port) before it merges.
- `/close-out` runs before the project is considered done.
