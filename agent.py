from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque


class AgentError(Exception):
    """Base exception for agent failures."""


class AgentTimeoutError(AgentError):
    """Watchdog fired; the whole process group has already been killed."""


class AgentProcessError(AgentError):
    """Nonzero exit, unparseable output, or the CLI's own is_error:true."""


class AgentValidationError(AgentError):
    """structured_output failed local schema validation."""


_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "LANG",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
)


def _build_subprocess_env() -> dict[str, str]:
    """Return a minimal, allowlisted environment for the Claude CLI child.

    Claude is a Node process, so NODE_OPTIONS=--require /tmp/x.js reaching the
    child is direct code execution. Because this is an allowlist, NODE_OPTIONS,
    LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH, and app secrets such as
    SESSION_SECRET or ADMIN_PASSWORD_HASH are excluded automatically.
    `_ENV_ALLOWLIST` must never be widened to include NODE_OPTIONS.
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST and v}
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    return env


def _platform_popen_kwargs() -> dict:
    """Return platform-specific kwargs so we can later kill the whole tree.

    Cites cleo_llm_handler.py:565-575 -- start_new_session is the POSIX original
    that lets killpg act on grandchildren, not just the direct child.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _signal_graceful(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # CTRL_BREAK_EVENT is genuinely best-effort and may be ignored.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _kill_forceful(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # /T walks the real Windows parent-child PID tree and /F forces it.
        # A plain parent-PID kill does not take down its children on Windows.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass  # taskkill may fail if the process already died; ignore that
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _reap_with_ladder(proc: subprocess.Popen, *, grace_s: float = 5.0) -> None:
    """Graceful -> wait -> forceful -> wait.

    NEVER call a hard kill unconditionally on timeout -- that only ever
    signals proc's single own PID, never anything it spawned, which is exactly
    the orphaned-grandchild bug this function exists to prevent.
    Cites cleo_llm_handler.py:502-539's _reap.
    """
    if proc.poll() is not None:
        # Guard against re-signalling an already-reaped, possibly-recycled PID.
        # Cites cleo_llm_handler.py:472-499's _signal_group guard.
        return
    _signal_graceful(proc)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_forceful(proc)
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass  # best-effort past this point; caller proceeds regardless


def _scrub_error_text(text: str, limit: int = 250) -> str:
    text = (text or "").strip()
    text = re.sub(r"[A-Za-z0-9_-]{20,}", "[redacted]", text)
    return text[:limit]


def _validate_against_schema(value: object, schema: dict, path: str = "$") -> None:
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(value, dict):
            raise AgentValidationError(f"{path}: expected object")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    raise AgentValidationError(f"{path}: extra property {key!r}")
        for name in schema.get("required", []):
            if name not in value:
                raise AgentValidationError(f"{path}: required property {name!r} missing")
        for key, subschema in properties.items():
            if key in value:
                _validate_against_schema(value[key], subschema, f"{path}.{key}")
    elif stype == "array":
        if not isinstance(value, list):
            raise AgentValidationError(f"{path}: expected array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise AgentValidationError(f"{path}: array exceeds maxItems")
        items = schema.get("items")
        for idx, item in enumerate(value):
            _validate_against_schema(item, items, f"{path}[{idx}]")
    elif stype == "string":
        if not isinstance(value, str):
            raise AgentValidationError(f"{path}: expected string")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise AgentValidationError(f"{path}: string exceeds maxLength")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise AgentValidationError(f"{path}: string does not match pattern")
        if "enum" in schema and value not in schema["enum"]:
            raise AgentValidationError(f"{path}: string not in enum")


def run_claude(
    system: str,
    prompt: str,
    schema: dict,
    *,
    model: str = "claude-sonnet-5",
    timeout_s: float = 120.0,
) -> dict:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    resolved = shutil.which(claude_bin)
    if resolved is None:
        raise AgentProcessError(
            f"claude CLI not found: {claude_bin!r} (check PATH or CLAUDE_BIN)"
        )

    cwd = tempfile.mkdtemp(prefix="dt-agent-")
    try:
        # Headless claude walks up from cwd discovering .claude/settings.json and
        # CLAUDE.md; .claude/settings.json can define hooks. A dedicated fresh
        # empty temp dir has neither, closing that hole.
        cmd = [
            resolved,
            "-p",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--tools",
            "",
            "--output-format",
            "json",
            "--model",
            model,
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(schema),
            "--",
            prompt,
        ]
        # --strict-mcp-config is required together with --tools "".
        # --tools alone does not stop headless claude from loading user-scope
        # mcpServers and leaking docker containers. The -- must be second-to-last,
        # immediately before prompt, unconditionally: a cheap, load-bearing habit.

        env = _build_subprocess_env()
        popen_kwargs = _platform_popen_kwargs()

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: deque[str] = deque(maxlen=200)

        def _drain(stream, sink) -> None:
            try:
                for line in stream:
                    sink.append(line)
            except (ValueError, OSError):
                pass  # pipe closed under us; stop draining

        out_thread = threading.Thread(
            target=_drain, args=(proc.stdout, stdout_chunks), daemon=True
        )
        err_thread = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
        )
        out_thread.start()
        err_thread.start()

        try:
            proc.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
        # This single wait(timeout=...) call IS the watchdog. Unlike cleo's
        # streaming design, the main thread has nothing else to do while waiting.

        _reap_with_ladder(proc, grace_s=5.0)

        out_thread.join(timeout=5.0)
        err_thread.join(timeout=5.0)
        proc.stdout.close()
        proc.stderr.close()

        if timed_out:
            raise AgentTimeoutError(f"claude CLI exceeded {timeout_s:.0f}s timeout")

        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)

        if proc.returncode != 0:
            raise AgentProcessError(_scrub_error_text(stderr_text or stdout_text))

        stdout_text = stdout_text.strip()
        if not stdout_text:
            raise AgentProcessError("claude CLI produced no output")
        try:
            envelope = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise AgentProcessError(_scrub_error_text(stdout_text)) from exc

        if envelope.get("is_error"):
            result_msg = str(envelope.get("result") or "claude CLI reported an error")
            raise AgentProcessError(_scrub_error_text(result_msg))

        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            try:
                structured = json.loads(envelope["result"])
            except (KeyError, json.JSONDecodeError) as exc:
                raise AgentProcessError(
                    "claude CLI response had no structured_output"
                ) from exc

        _validate_against_schema(structured, schema)
        return structured
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
