from __future__ import annotations

import importlib


def test_init_db_is_idempotent(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    db.init_db()  # must not raise on a second boot

    with db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    for expected in (
        "projects", "items", "swatches", "syntheses", "decisions",
        "jobs", "media", "sessions", "login_attempts", "rate_limits",
    ):
        assert expected in tables


def test_init_db_creates_media_dir(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    assert db.MEDIA_DIR.is_dir()


def test_update_item_rejects_non_allowlisted_columns(app_env):
    import db

    importlib.reload(db)
    db.init_db()
    try:
        db.update_item(1, source_url_injected="'; DROP TABLE items; --")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-allowlisted column")
