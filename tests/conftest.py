from __future__ import annotations

import http.server
import inspect
import ipaddress
import json
import sys
import threading
from pathlib import Path

import pytest

import net_guard

_STRUCTURED_OUTPUT_DEFAULT = object()


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    impl_path = tmp_path / "fake_claude_impl.py"
    recipe_path = tmp_path / "fake_claude_recipe.json"

    impl_source = inspect.cleandoc(
        """
        from __future__ import annotations

        import json
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        _RECIPE_PATH = Path(__file__).resolve().parent / "fake_claude_recipe.json"
        _DUMP_PATH = Path(__file__).resolve().parent / "fake_claude_dump.json"


        def _write_dump(extra: dict | None = None) -> None:
            state = {"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()}
            if extra:
                state.update(extra)
            _DUMP_PATH.write_text(json.dumps(state), encoding="utf-8")


        def main() -> None:
            _write_dump()
            recipe = json.loads(_RECIPE_PATH.read_text(encoding="utf-8"))
            try:
                stdin_data = sys.stdin.read()
            except Exception:
                stdin_data = ""
            _write_dump({"stdin": stdin_data})
            if recipe.get("hang"):
                if sys.platform == "win32":
                    try:
                        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
                    except (OSError, ValueError):
                        pass
                else:
                    try:
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    except (OSError, ValueError):
                        pass
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(9999)"]
                )
                _write_dump({"child_pid": child.pid})
                time.sleep(9999)
            else:
                stderr = recipe.get("stderr", "")
                if stderr:
                    sys.stderr.write(stderr)
                    sys.stderr.flush()
                exit_code = recipe.get("exit_code", 0)
                if exit_code:
                    sys.exit(exit_code)
                if recipe.get("stream_json"):
                    for event in recipe.get("stream_events", []):
                        if isinstance(event, dict):
                            print(json.dumps(event))
                        else:
                            print(event)
                        sys.stdout.flush()
                    print(json.dumps(recipe["envelope"]))
                    sys.stdout.flush()
                    sys.exit(0)
                else:
                    print(json.dumps(recipe["envelope"]))
                    sys.exit(0)


        if __name__ == "__main__":
            main()
        """
    )
    impl_path.write_text(impl_source, encoding="utf-8")

    if sys.platform == "win32":
        # A .cmd launcher can't be used here: cmd.exe's command-line reader is
        # line-based and corrupts/truncates any argument containing an embedded
        # newline even inside quotes (verified empirically) -- and the real
        # multi-line prompts this driver sends (item digests, page text) would
        # get silently truncated before the wrapped script ever saw them.
        # PowerShell's argv handling doesn't have that limitation, so the fixture
        # emulates the real CLI as a .ps1 script instead; agent.py's
        # _command_for() knows to invoke a resolved .ps1 via `powershell -File`.
        launcher_path = tmp_path / "fake_claude.ps1"
        py_exe = sys.executable.replace("'", "''")
        impl = str(impl_path).replace("'", "''")
        launcher_body = f"& '{py_exe}' '{impl}' @args\n"
    else:
        launcher_path = tmp_path / "fake_claude"
        shebang = f"#!{sys.executable}"
        runpy_line = f"import runpy; runpy.run_path(r'{impl_path}', run_name='__main__')"
        launcher_body = f"{shebang}\n{runpy_line}\n"
        launcher_path.chmod(0o755)
    launcher_path.write_text(launcher_body, encoding="utf-8")

    def configure(
        *,
        structured_output: dict | None = None,
        is_error: bool = False,
        result: str | None = None,
        structured_output_field: object = _STRUCTURED_OUTPUT_DEFAULT,
        exit_code: int = 0,
        stderr: str = "",
        hang: bool = False,
        stream_json: bool = False,
        stream_events: list[dict | str] | None = None,
    ) -> None:
        if structured_output_field is _STRUCTURED_OUTPUT_DEFAULT:
            structured_output_field = structured_output
        if result is None:
            result = json.dumps(structured_output) if structured_output is not None else ""
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "structured_output": structured_output_field,
            "result": result,
        }
        if stream_json and stream_events is None:
            stream_events = [
                {"type": "system", "subtype": "init", "session_id": "fake-session"},
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "(thinking)"}],
                    },
                },
            ]
        recipe = {
            "envelope": envelope,
            "exit_code": exit_code,
            "stderr": stderr,
            "hang": hang,
            "stream_json": stream_json,
            "stream_events": stream_events or [],
        }
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_BIN", str(launcher_path))

    return configure


@pytest.fixture()
def app_env(monkeypatch, tmp_path: Path):
    """Set up an isolated env + temp DB dir before app.py is imported."""
    from auth import hash_password

    monkeypatch.setenv("ADMIN_USER", "captain")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("test-password-please-1234"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-prod")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    yield


@pytest.fixture()
def client(app_env):
    import importlib

    import db as db_module

    importlib.reload(db_module)
    import app as app_module

    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def logged_in_client(client):
    resp = client.post(
        "/login", data={"username": "captain", "password": "test-password-please-1234"}
    )
    assert resp.status_code == 302
    return client


@pytest.fixture()
def local_http_server():
    """Factory: local_http_server(handler_cls) starts a ThreadingHTTPServer bound to
    127.0.0.1 on an OS-assigned ephemeral port using the given
    http.server.BaseHTTPRequestHandler subclass, and returns its base url
    ('http://127.0.0.1:<port>'). Every server started during the test is shut down
    automatically at teardown. Each test defines its own handler_cls (overriding
    do_GET) to control exactly what routes/status codes/headers/bodies it serves."""
    servers = []

    def _make(handler_cls) -> str:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _make

    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def guard_allow_loopback(monkeypatch):
    """Returns allow(port): patches net_guard so 127.0.0.1 is treated as a public
    address and `port` is added to the allowed-port set, while leaving every other
    part of the real guard logic untouched -- DNS resolution, hostname/scheme/
    credential checks, and forbidden-address checks for every OTHER address
    (including redirect targets) all still run for real. The real guard would
    otherwise correctly refuse to fetch a local test server on BOTH the loopback-
    address check and the 80/443 port allowlist (test servers bind an ephemeral,
    non-standard port); this narrowly punches through just enough to let tests
    exercise real DNS/redirect-revalidation logic against a real local server
    without weakening the guard for anything except 127.0.0.1 itself on the one
    port the test explicitly allowed."""

    def _allow(port: int) -> None:
        real_forbidden = net_guard._addr_is_forbidden
        loopback = ipaddress.ip_address("127.0.0.1")

        def patched(ip):
            if ip == loopback:
                return False
            return real_forbidden(ip)

        monkeypatch.setattr(net_guard, "_addr_is_forbidden", patched)
        monkeypatch.setattr(net_guard, "_ALLOWED_PORTS", net_guard._ALLOWED_PORTS | {port})

    return _allow