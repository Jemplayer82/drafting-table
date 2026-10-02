# Deploy

Run the pre-built image. The compose file points at `ghcr.io/jemplayer82/drafting-table:latest`
and never uses `build: .`. CI builds and pushes on every push to `main`, after a gitleaks secret
scan and the test suite pass.

Start both services:

```bash
docker compose up -d
```

There are two services from one image:

- `web` (gunicorn) serves the pages and the drop endpoint.
- `worker` is a separate process that runs the agent pipeline.

Job state lives in SQLite (WAL mode). Uploaded images and thumbnails live on a separate volume,
so a flood of media can't take the database down with it.

## Secrets in production

> **Secrets in production:** with an unauthenticated Portainer API on the deploy host's LAN, stack
> environment variables are readable by anyone who can reach it. Mount `ADMIN_PASSWORD_HASH`,
> `SESSION_SECRET`, and `CLAUDE_CODE_OAUTH_TOKEN` as files instead of plain compose env vars —
> the app reads a `<VAR>_FILE` path first if one's set.

> **Not an API key.** `CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token` and bills against
> your Claude subscription, not per-token API usage. Don't confuse it with `ANTHROPIC_API_KEY`.
