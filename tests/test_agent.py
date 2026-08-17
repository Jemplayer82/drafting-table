from __future__ import annotations

import inspect
import json
import os
import pathlib
import subprocess
import sys

import pytest

import agent

SIMPLE_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "additionalProperties": False,
    "properties": {"status": {"type": "string", "maxLength": 20}},
}


@pytest.fixture
def fake_claude(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
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
                print(json.dumps(recipe["envelope"]))
                sys.exit(0)


        if __name__ == "__main__":
            main()
        """
    )
    impl_path.write_text(impl_source, encoding="utf-8")

    if sys.platform == "win32":
        launcher_path = tmp_path / "fake_claude.cmd"
        launcher_body = f'@echo off\n"{sys.executable}" "{impl_path}" %*\n'
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
        exit_code: int = 0,
        stderr: str = "",
        hang: bool = False,
    ) -> None:
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": structured_output,
            "result": json.dumps(structured_output) if structured_output is not None else "",
        }
        recipe = {
            "envelope": envelope,
            "exit_code": exit_code,
            "stderr": stderr,
            "hang": hang,
        }
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_BIN", str(launcher_path))

    return configure


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def test_run_claude_happy_path_parses_structured_output(fake_claude) -> None:
    fake_claude(structured_output={"status": "ready"})
    result = agent.run_claude("sys", "prompt", SIMPLE_SCHEMA)
    assert result == {"status": "ready"}


def test_run_claude_rejects_schema_violating_response_without_coercing(fake_claude) -> None:
    fake_claude(structured_output={"status": "x" * 999})
    with pytest.raises(agent.AgentValidationError):
        agent.run_claude("sys", "prompt", SIMPLE_SCHEMA)


def test_run_claude_nonzero_exit_raises_scrubbed_message(fake_claude) -> None:
    fake_claude(exit_code=1, stderr="boom token=" + "A" * 32)
    with pytest.raises(agent.AgentProcessError) as exc_info:
        agent.run_claude("sys", "prompt", SIMPLE_SCHEMA)
    message = str(exc_info.value)
    assert "A" * 32 not in message
    assert len(message) <= 260


def test_run_claude_timeout_kills_whole_process_tree(fake_claude, tmp_path) -> None:
    fake_claude(hang=True)
    with pytest.raises(agent.AgentTimeoutError):
        agent.run_claude("sys", "prompt", {"type": "object"}, timeout_s=2.0)
    dump_path = tmp_path / "fake_claude_dump.json"
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    assert not _pid_alive(dump["pid"])
    assert "child_pid" in dump
    assert not _pid_alive(dump["child_pid"])


def test_run_claude_env_excludes_dangerous_vars_even_when_set_in_test_environment(
    fake_claude, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    monkeypatch.setenv("SESSION_SECRET", "super-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    fake_claude(structured_output={"status": "ready"})
    agent.run_claude("sys", "prompt", SIMPLE_SCHEMA)
    dump_path = tmp_path / "fake_claude_dump.json"
    env = json.loads(dump_path.read_text(encoding="utf-8"))["env"]
    assert "NODE_OPTIONS" not in env
    assert "PYTHONPATH" not in env
    assert "SESSION_SECRET" not in env
    assert env.get("NO_COLOR") == "1"
    assert env.get("CI") == "1"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "test-token"


def test_run_claude_argv_places_double_dash_immediately_before_prompt(
    fake_claude, tmp_path
) -> None:
    fake_claude(structured_output={"status": "ready"})
    distinctive = "distinctive-prompt-42"
    agent.run_claude("sys", distinctive, SIMPLE_SCHEMA)
    dump_path = tmp_path / "fake_claude_dump.json"
    argv = json.loads(dump_path.read_text(encoding="utf-8"))["argv"]
    assert argv[-2] == "--"
    assert argv[-1] == distinctive
