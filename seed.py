from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

SEED_HTML_PATH = Path(__file__).resolve().parent / "seed" / "artifact-source.html"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _parse_swatches(ref_div: Tag) -> list[dict]:
    swatches: list[dict] = []
    for sw in ref_div.select(".ref-swatch"):
        style = sw.get("style", "")
        m = re.search(r"#[0-9A-Fa-f]{6}", style)
        if not m:
            continue
        hex_value = m.group(0)
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_value):
            continue
        span = sw.find("span")
        label = span.get_text(strip=True) if span else hex_value
        swatches.append({"hex": hex_value, "label": label})
    return swatches


def _parse_ref(ref_div: Tag) -> dict:
    title_el = ref_div.select_one(".ref-url")
    title = title_el.get_text(strip=True) if title_el else None

    tag_el = ref_div.select_one(".ref-tag")
    tag = tag_el.get_text(strip=True) if tag_el else None

    note_el = ref_div.select_one(".ref-note")
    note = note_el.get_text(strip=True) if note_el else None

    link = ref_div.select_one("a.ref-thumb-link")
    img_el = ref_div.select_one("img.ref-thumb")

    if img_el is not None:
        kind = "image"
        source_url = link.get("href") if link else None
        alt_text = img_el.get("alt") or None
        image_data_uri = img_el.get("src")
    else:
        kind = "note"
        source_url = alt_text = image_data_uri = None

    return {
        "title": title,
        "tag": tag,
        "note": note,
        "kind": kind,
        "source_url": source_url,
        "alt_text": alt_text,
        "image_data_uri": image_data_uri,
        "swatches": _parse_swatches(ref_div),
    }


def _parse_bullets(project_div: Tag, selector: str) -> list[str]:
    return [li.get_text(strip=True) for li in project_div.select(selector)]


def _parse_decisions(project_div: Tag) -> list[dict]:
    decisions: list[dict] = []
    for li in project_div.select(".decisions li"):
        strong = li.find("strong")
        if strong is not None:
            rationale_md = f"**{strong.get_text(strip=True)}**"
            strong.extract()
            body_md = li.get_text(strip=True)
        else:
            rationale_md = None
            body_md = li.get_text(strip=True)
        decisions.append({"body_md": body_md, "rationale_md": rationale_md})
    return decisions


def parse_source(html_path: Path = SEED_HTML_PATH) -> list[dict]:
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    projects: list[dict] = []
    for project_div in soup.select(".project"):
        name = project_div.select_one(".project-name").get_text(strip=True)
        projects.append(
            {
                "name": name,
                "slug": _slugify(name),
                "items": [_parse_ref(r) for r in project_div.select(".ref")],
                "ideas": _parse_bullets(project_div, ".ideas li"),
                "questions": _parse_bullets(project_div, ".questions li"),
                "decisions": _parse_decisions(project_div),
            }
        )
    return projects
