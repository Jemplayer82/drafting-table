from __future__ import annotations

import json
import re

import pytest

import seed


def test_parses_two_projects_in_order():
    projects = seed.parse_source()
    assert len(projects) == 2
    assert projects[0]["name"] == "Studio Portfolio Site"
    assert projects[0]["slug"] == "studio-portfolio-site"
    assert projects[1]["name"] == "jemplayer82 / Web Design Ideas"
    assert projects[1]["slug"] == "jemplayer82-web-design-ideas"


def test_example_project_has_2_note_items():
    example = seed.parse_source()[0]
    assert len(example["items"]) == 2
    assert all(item["kind"] == "note" for item in example["items"])
    assert all(item["image_data_uri"] is None for item in example["items"])
    assert all(item["source_url"] is None for item in example["items"])


def test_real_project_has_18_image_items_with_correct_tags():
    real = seed.parse_source()[1]
    assert len(real["items"]) == 18
    expected_tags = [
        "layout + type",
        "dark + immersive photo",
        "dark + accent glow",
        "hero concept",
        "palette",
        "grid layout",
        "texture + palette",
        "dark luxury",
        "texture + type",
        "palette",
        "palette + brand story",
        "palette + accent pop",
        "palette",
        "palette",
        "palette + naming system",
        "palette",
        "palette + texture",
        "palette + naming system",
    ]
    assert [item["tag"] for item in real["items"]] == expected_tags

    for item in real["items"]:
        assert item["kind"] == "image"
        assert item["image_data_uri"] is not None
        assert item["image_data_uri"].startswith("data:image/")
        assert item["note"]

    title0 = real["items"][0]["title"] or ""
    assert "mntn" in title0.lower()
    assert "hiking guide" in title0.lower()

    title3 = real["items"][3]["title"] or ""
    assert "double-exposure slider" in title3.lower()

    title4 = real["items"][4]["title"] or ""
    assert "color palette" in title4.lower()
    assert "gold" in title4.lower()
    assert "dark teal" in title4.lower()

    title9 = real["items"][9]["title"] or ""
    assert "emerald" in title9.lower()
    assert "wasabi" in title9.lower()
    assert "khaki" in title9.lower()

    title13 = real["items"][13]["title"] or ""
    assert "midnight tide" in title13.lower()

    title17 = real["items"][17]["title"] or ""
    assert "sea glass" in title17.lower()
    assert "beach mist" in title17.lower()
    assert "coastal slate" in title17.lower()


def test_swatches_parsed_with_valid_hex_and_labels():
    real = seed.parse_source()[1]
    color_item = next(
        item for item in real["items"] if "color palette" in (item["title"] or "").lower()
    )
    assert color_item["swatches"] == [
        {"hex": "#D5891B", "label": "D5891B"},
        {"hex": "#7F3A0E", "label": "7F3A0E"},
        {"hex": "#542409", "label": "542409"},
        {"hex": "#17110D", "label": "17110D"},
        {"hex": "#0B282A", "label": "0B282A"},
        {"hex": "#148A88", "label": "148A88"},
    ]

    emerald_item = next(
        item for item in real["items"] if "emerald" in (item["title"] or "").lower()
    )
    assert {"hex": "#284139", "label": "Emerald"} in emerald_item["swatches"]

    total_swatches = sum(len(item["swatches"]) for item in real["items"])
    assert total_swatches == 44

    for item in real["items"]:
        for swatch in item["swatches"]:
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", swatch["hex"])


def test_ideas_and_questions_parsed():
    example, real = seed.parse_source()
    assert len(example["ideas"]) == 2
    assert len(example["questions"]) == 2
    assert len(real["ideas"]) == 9
    assert len(real["questions"]) == 5
    assert "palette-hunting" in real["ideas"][5].lower()
    assert real["questions"][0].lower().startswith("which single accent color")


def test_decisions_parsed_with_label_split():
    example = seed.parse_source()[0]
    assert len(example["decisions"]) == 2
    assert example["decisions"][0] == {
        "body_md": "serif display + grotesk body, confirmed after comparing three pairings.",
        "rationale_md": "**Type:**",
    }
    assert example["decisions"][1]["rationale_md"] == "**Nav:**"

    real = seed.parse_source()[1]
    assert real["decisions"] == []


@pytest.fixture()
def seeded_db(app_env):
    import importlib

    import db as db_module
    import seed as seed_module

    importlib.reload(db_module)
    db_module.init_db()
    return db_module, seed_module


def test_seed_is_idempotent(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        first = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert first == 20

    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        second = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert second == 20


def test_seed_imports_both_projects_into_db(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        rows = conn.execute("SELECT slug FROM projects").fetchall()
    assert {row["slug"] for row in rows} == {
        "studio-portfolio-site",
        "jemplayer82-web-design-ideas",
    }


def test_seed_real_project_items_have_correct_fields(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        project_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()["id"]
        rows = conn.execute(
            "SELECT title, tag, kind, status, media_id, thumb_media_id "
            "FROM items WHERE project_id = ?",
            (project_id,),
        ).fetchall()

    assert len(rows) == 18
    assert all(row["kind"] == "image" for row in rows)
    assert all(row["status"] == "ready" for row in rows)

    mntn = next(row for row in rows if "mntn" in (row["title"] or "").lower())
    assert mntn["tag"] == "layout + type"
    assert mntn["media_id"] is not None
    assert mntn["thumb_media_id"] is not None


def test_seed_example_project_items_have_no_media(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        project_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?",
            ("studio-portfolio-site",),
        ).fetchone()["id"]
        rows = conn.execute(
            "SELECT kind, media_id, thumb_media_id FROM items WHERE project_id = ?",
            (project_id,),
        ).fetchall()

    assert len(rows) == 2
    assert all(row["kind"] == "note" for row in rows)
    assert all(row["media_id"] is None for row in rows)
    assert all(row["thumb_media_id"] is None for row in rows)


def test_seed_swatches_in_db_have_valid_hex(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()
    with db_module.connect() as conn:
        rows = conn.execute("SELECT hex FROM swatches").fetchall()

    assert len(rows) == 44
    for row in rows:
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", row["hex"])


def test_seed_syntheses_and_decisions_rows(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()

    with db_module.connect() as conn:
        real_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?",
            ("jemplayer82-web-design-ideas",),
        ).fetchone()["id"]
        synth = conn.execute(
            "SELECT version, direction_md, questions_json FROM syntheses "
            "WHERE project_id = ?",
            (real_id,),
        ).fetchone()
        real_decisions = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE project_id = ?",
            (real_id,),
        ).fetchone()["n"]

        example_id = conn.execute(
            "SELECT id FROM projects WHERE slug = ?",
            ("studio-portfolio-site",),
        ).fetchone()["id"]
        example_decisions = conn.execute(
            "SELECT source, status FROM decisions WHERE project_id = ?",
            (example_id,),
        ).fetchall()

    assert synth is not None
    assert synth["version"] == 1
    assert "palette-hunting" in synth["direction_md"]
    questions = json.loads(synth["questions_json"])
    assert len(questions) == 5
    assert all(q == {"question": q["question"], "why": ""} for q in questions)

    assert real_decisions == 0
    assert len(example_decisions) == 2
    assert all(row["source"] == "user" for row in example_decisions)
    assert all(row["status"] == "accepted" for row in example_decisions)


def test_seed_media_files_written_to_disk(seeded_db):
    db_module, seed_module = seeded_db
    seed_module.run_seed_if_empty()

    with db_module.connect() as conn:
        item_row = conn.execute(
            "SELECT media_id, thumb_media_id FROM items "
            "WHERE media_id IS NOT NULL LIMIT 1"
        ).fetchone()
        full_media = conn.execute(
            "SELECT path, mime, width, height, byte_size FROM media WHERE id = ?",
            (item_row["media_id"],),
        ).fetchone()
        thumb_media = conn.execute(
            "SELECT path, mime, width, height, byte_size FROM media WHERE id = ?",
            (item_row["thumb_media_id"],),
        ).fetchone()

    full_path = db_module.MEDIA_DIR / full_media["path"]
    thumb_path = db_module.MEDIA_DIR / thumb_media["path"]

    assert full_path.is_file()
    assert thumb_path.is_file()
    assert full_path.stat().st_size == full_media["byte_size"]
    assert thumb_path.stat().st_size == thumb_media["byte_size"]

    assert full_media["mime"] == "image/jpeg"
    assert thumb_media["mime"] == "image/webp"
    assert full_media["width"] > 0 and full_media["height"] > 0
    assert thumb_media["width"] > 0 and thumb_media["height"] > 0
