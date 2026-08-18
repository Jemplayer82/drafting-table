# syntax=docker/dockerfile:1
#
# Three-stage build. node-builder and py-builder are pure build tooling and
# do not survive into the final image; final only carries what's actually
# needed at runtime.

# --- Stage 1: install the pinned Claude Code CLI using Node copied in from
# the official node image (not apt-installed -- Debian's apt node is stale,
# and this avoids adding NodeSource as a second package-manager surface).
FROM python:3.12-slim AS node-builder

# npm/npx under /usr/local/bin are RELATIVE symlinks into
# /usr/local/lib/node_modules/npm/bin/*.js. `COPY --from=X /usr/local/bin/npm dest`
# DEREFERENCES that symlink into a flattened regular *file* at the destination --
# confirmed empirically: the copied file crashes immediately with
# `Error: Cannot find module '../lib/cli.js'` because npm-cli.js's relative
# require() no longer resolves from its original location once flattened.
# Fix: copy the whole npm package directory (preserves internal structure),
# then recreate the bin symlinks locally so the relative path is correct again.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# PINNED EXACTLY per the plan doc: "a silent upgrade changing either becomes
# 'the board stopped filling in cards' three weeks later." Do not use
# ^2.1.203, ~2.1.203, or @latest.
RUN npm install -g @anthropic-ai/claude-code@2.1.203 \
 && claude --version

# --- Stage 2: resolve Python deps from the committed lockfile. Nothing
# app-specific here -- just pyproject.toml + uv.lock -- so this stage stays
# cached across pure app-code changes.
FROM python:3.12-slim AS py-builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# [tool.uv] package = false in pyproject.toml -- this is a flat script
# collection, not an installable package, so plain `uv sync --frozen --no-dev`
# is correct and sufficient (verified: produces exactly flask/gunicorn/httpx/
# pillow/beautifulsoup4 + their transitive deps, no pytest/ruff).
RUN uv sync --frozen --no-dev

# --- Stage 3: final runtime image.
FROM python:3.12-slim AS final

# curl: required by docker-compose.yaml's existing healthcheck
# (CMD curl -fs http://localhost:8093/healthz) -- python:3.12-slim doesn't
# ship it. (ca-certificates, needed by httpx for TLS in net_guard.py's
# fetches, is already present in this base image -- confirmed via apt-get
# output: "ca-certificates is already the newest version" -- so no separate
# install is needed for that.)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Verified empirically with a real build+run: @anthropic-ai/claude-code@2.1.203's
# installed bin/claude.exe is a self-contained ELF executable (confirmed via
# file magic bytes: \x7fELF), NOT a Node.js script -- it runs standalone with
# no system node/npm/npx on PATH at all. A build carrying only this package
# directory + one symlink, with node never copied into that stage, produced a
# working `claude --version`. So only the installed package directory + its
# bin symlink need to survive into final -- the node binary and npm itself
# are build-time-only tooling for this particular image and are deliberately
# left out of the final stage to keep it lean.
COPY --from=node-builder /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude

WORKDIR /app
COPY --from=py-builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# gunicorn 26's control-socket feature defaults to a root-owned path
# (/.gunicorn/gunicorn.ctl) that the non-root UID below can't create.
# Confirmed empirically: without this, gunicorn logs a non-fatal
# `[ERROR] Control server error: [Errno 13] Permission denied: '/.gunicorn'`
# on every boot even though the server itself still starts and serves
# correctly (/healthz returns ok). GUNICORN_CMD_ARGS is gunicorn's own
# mechanism for injecting extra args at startup regardless of how it's
# invoked, so this reaches gunicorn without touching docker-compose.yaml's
# command: line. Confirmed the fix works: with this set, the same boot
# produces no error line. worker.py never runs gunicorn, so this is a no-op
# for that service.
ENV GUNICORN_CMD_ARGS="--no-control-socket"

COPY app.py db.py auth.py net_guard.py agent.py worker.py media.py seed.py hashpw.py ./
COPY templates/ templates/
COPY static/ static/
COPY seed/ seed/

# Pre-create and hand over /data + /media BEFORE switching USER: app.py runs
# db.init_db() + seed.run_seed_if_empty() at *module import time*, and
# db.init_db() does DATA_DIR.mkdir(parents=True)/MEDIA_DIR.mkdir(...) -- a
# non-root UID cannot create a new dir under a root-owned /.
RUN mkdir -p /data /media && chown -R 10001:10001 /data /media /app

USER 10001:10001
EXPOSE 8093
# docker-compose.yaml already overrides `command:` explicitly for both
# services (web: gunicorn, worker: python -m worker) -- this default only
# matters for an ad-hoc `docker run` with no override.
CMD ["gunicorn", "app:app", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8093", "--timeout", "60"]
