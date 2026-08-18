"""Shared image validation, re-encoding, and storage pipeline.

Every image that enters the app is validated by magic-byte sniffing, screened
for decompression bombs, and re-encoded before storage. SVG is rejected
explicitly rather than handed to Pillow.
"""

from __future__ import annotations

import secrets
import warnings
from io import BytesIO

from PIL import Image

import db

Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


class MediaValidationError(Exception):
    """Raised when an image cannot be accepted.

    The message is always short and safe to show a client directly; it is
    never a raw Pillow exception or stack trace.
    """


def _sniff_format(data: bytes) -> str | None:
    """Return 'jpeg', 'png', 'webp', or 'gif' based on magic bytes only.

    Returns None if the leading bytes do not match a supported image format.
    File extensions and caller-supplied Content-Types are ignored.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _looks_like_svg(data: bytes) -> bool:
    """Return True if the leading bytes look like an SVG document.

    Only the first ~512 bytes are inspected, after stripping a UTF-8 BOM and
    any leading whitespace. The check is case-insensitive.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.lstrip()
    preview = data[:512]
    try:
        text = preview.decode("utf-8", errors="replace").lower()
    except UnicodeDecodeError:
        return False
    return "<?xml" in text or "<svg" in text


def decode_and_validate(data: bytes) -> Image.Image:
    """Decode and validate image bytes, returning a Pillow Image.

    Frame-0-only for animated GIF/WebP requires no special code:
    Image.open() is lazy and only decodes the current (0th) frame until
    something calls .seek(), iterates frames, or passes save_all=True.
    """
    if _looks_like_svg(data):
        raise MediaValidationError("SVG images are not supported")

    fmt = _sniff_format(data)
    if fmt is None:
        raise MediaValidationError("unrecognized image format")

    try:
        img = Image.open(BytesIO(data))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaValidationError("image exceeds the maximum allowed pixel dimensions") from exc
    except Exception as exc:
        raise MediaValidationError("could not read image data") from exc

    width, height = img.size
    if width * height > Image.MAX_IMAGE_PIXELS:
        raise MediaValidationError("image exceeds the maximum allowed pixel dimensions")

    try:
        img.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaValidationError("image exceeds the maximum allowed pixel dimensions") from exc
    except Exception as exc:
        raise MediaValidationError("could not decode image data") from exc

    return img


def _write_media_file(
    img: Image.Image, pillow_format: str, mime: str, ext: str, **save_kwargs: object
) -> str:
    """Save a Pillow image to disk and record it in the media table.

    Converts to RGB if necessary. Only the current frame is saved unless
    save_all=True is passed. The file id is generated with
    secrets.token_hex(16); the relative path is never derived from user input.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")

    media_id = secrets.token_hex(16)
    rel_path = f"{media_id[:2]}/{media_id}.{ext}"
    abs_path = db.MEDIA_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    buf = BytesIO()
    img.save(buf, format=pillow_format, **save_kwargs)
    data = buf.getvalue()
    abs_path.write_bytes(data)

    db.insert_media(media_id, rel_path, mime, img.width, img.height, len(data))
    return media_id


def store_full_image(img: Image.Image) -> str:
    """Store a full-size re-encoded JPEG capped at 1600px on the long edge.

    Returns the new media_id.
    """
    full = img.copy()
    full.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    return _write_media_file(full, "JPEG", "image/jpeg", "jpg", quality=90)


def store_thumbnail(img: Image.Image) -> tuple[str, int, int]:
    """Store a WebP thumbnail capped at 640px on the long edge.

    Returns (thumb_media_id, width, height).
    """
    thumb = img.copy()
    thumb.thumbnail((640, 640), Image.Resampling.LANCZOS)
    media_id = _write_media_file(thumb, "WEBP", "image/webp", "webp", quality=82)
    return media_id, thumb.width, thumb.height


def store_image_pair(img: Image.Image) -> dict:
    """Store a full image and its thumbnail, returning the usual dict shape.

    Returns {'media_id', 'thumb_media_id', 'thumb_w', 'thumb_h'} matching
    worker.py's existing _try_fetch_thumbnail success-return shape.
    """
    media_id = store_full_image(img)
    thumb_media_id, thumb_w, thumb_h = store_thumbnail(img)
    return {
        "media_id": media_id,
        "thumb_media_id": thumb_media_id,
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
    }
