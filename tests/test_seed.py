from __future__ import annotations

import re

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
