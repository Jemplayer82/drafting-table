from __future__ import annotations

import base64
import json
import re
import secrets
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from PIL import Image

import db

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


def run_seed_if_empty(html_path: Path = SEED_HTML_PATH) -> None:
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        if n:
            return
    if not html_path.exists():
        return

    projects = parse_source(html_path)
    with db.connect() as conn:
        conn.execute("BEGIN")
        try:
            for proj in projects:
                _insert_project(conn, proj)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _insert_project(conn, proj: dict) -> None:
    now = db._now()
    cur = conn.execute(
        "INSERT INTO projects (name, slug, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (proj["name"], proj["slug"], None, now, now),
    )
    project_id = cur.lastrowid

    for position, item in enumerate(proj["items"]):
        _insert_item(conn, project_id, item, position)

    if proj["ideas"] or proj["questions"]:
        direction_md = "\n".join(f"- {b}" for b in proj["ideas"])
        questions_json = json.dumps([{"question": q, "why": ""} for q in proj["questions"]])
        conn.execute(
            "INSERT INTO syntheses (project_id, version, direction_md, questions_json, "
            "model, item_count, trigger_item_id, job_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                1,
                direction_md,
                questions_json,
                None,
                len(proj["items"]),
                None,
                None,
                now,
            ),
        )

    for d in proj["decisions"]:
        conn.execute(
            "INSERT INTO decisions (project_id, body_md, rationale_md, source, status, "
            "superseded_by, job_id, created_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                d["body_md"],
                d["rationale_md"],
                "user",
                "accepted",
                None,
                None,
                now,
                now,
            ),
        )


def _insert_item(conn, project_id: int, item: dict, position: int) -> None:
    now = db._now()
    media_id = thumb_id = thumb_w = thumb_h = None
    if item["image_data_uri"]:
        media_id, thumb_id, thumb_w, thumb_h = _store_image(conn, item["image_data_uri"])

    cur = conn.execute(
        "INSERT INTO items (project_id, kind, status, source_url, title, tag, "
        "note_md, alt_text, raw_text, media_id, thumb_media_id, thumb_w, thumb_h, "
        "content_hash, position, error, job_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            item["kind"],
            "ready",
            item["source_url"],
            item["title"],
            item["tag"],
            item["note"],
            item["alt_text"],
            None,
            media_id,
            thumb_id,
            thumb_w,
            thumb_h,
            None,
            position,
            None,
            None,
            now,
            now,
        ),
    )
    item_id = cur.lastrowid

    for pos2, sw in enumerate(item["swatches"]):
        conn.execute(
            "INSERT INTO swatches (item_id, hex, label, position) VALUES (?, ?, ?, ?)",
            (item_id, sw["hex"], sw["label"], pos2),
        )


def _store_image(conn, data_uri: str) -> tuple[str, str, int, int]:
    _prefix, _sep, b64 = data_uri.partition(",")
    raw = base64.b64decode(b64)
    img = Image.open(BytesIO(raw))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")

    full_img = img.copy()
    full_img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    full_id = _write_media(conn, full_img, "JPEG", "image/jpeg", "jpg", quality=90)

    thumb_img = img.copy()
    thumb_img.thumbnail((640, 640), Image.Resampling.LANCZOS)
    thumb_id = _write_media(conn, thumb_img, "WEBP", "image/webp", "webp", quality=82)

    return full_id, thumb_id, thumb_img.width, thumb_img.height


def _write_media(conn, img, pillow_format: str, mime: str, ext: str, **save_kwargs) -> str:
    media_id = secrets.token_hex(16)
    buf = BytesIO()
    img.save(buf, format=pillow_format, **save_kwargs)
    data = buf.getvalue()

    rel_path = f"{media_id[:2]}/{media_id}.{ext}"
    abs_path = db.MEDIA_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)

    conn.execute(
        "INSERT INTO media (id, path, mime, width, height, byte_size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (media_id, rel_path, mime, img.width, img.height, len(data), db._now()),
    )
    return media_id
