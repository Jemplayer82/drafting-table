from __future__ import annotations

from pathlib import Path

import pytest


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