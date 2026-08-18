from __future__ import annotations

import importlib
import warnings
from io import BytesIO

import pytest
from PIL import Image

import db
import media


def _init_db() -> None:
    importlib.reload(db)
    db.init_db()


def _make_image_bytes(
    mode: str = "RGB",
    size: tuple[int, int] = (100, 100),
    color: tuple[int, int, int] | str = "red",
    fmt: str = "PNG",
) -> tuple[Image.Image, bytes]:
    img = Image.new(mode, size, color)
    buf = BytesIO()
    img.save(buf, format=fmt)
    return img, buf.getvalue()


def test_decode_and_validate_accepts_jpeg_png_webp(app_env):
    _init_db()
    for fmt in ("JPEG", "PNG", "WEBP"):
        _, data = _make_image_bytes(size=(120, 90), color="blue", fmt=fmt)
        img = media.decode_and_validate(data)
        assert img.size == (120, 90)


def test_store_full_image_reencodes_and_strips_exif(app_env):
    _init_db()
    img = Image.new("RGB", (100, 100), "red")
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x0110] = "TestModel"

    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    original = buf.getvalue()

    original_exif = Image.open(BytesIO(original)).getexif()
    assert original_exif.get(0x010F) == "TestMake"

    decoded = media.decode_and_validate(original)
    media_id = media.store_full_image(decoded)

    row = db.get_media(media_id)
    stored_path = db.MEDIA_DIR / row["path"]
    stored = stored_path.read_bytes()

    assert stored != original
    stored_exif = Image.open(BytesIO(stored)).getexif()
    assert 0x010F not in stored_exif


def test_decode_and_validate_rejects_svg_with_explicit_message(app_env, monkeypatch):
    _init_db()

    def fake_open(*args, **kwargs):
        raise AssertionError("Pillow Image.open should not be called for SVG")

    monkeypatch.setattr(media.Image, "open", fake_open)

    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    with pytest.raises(media.MediaValidationError, match="SVG"):
        media.decode_and_validate(svg)


def test_decode_and_validate_rejects_wrong_magic_bytes(app_env):
    _init_db()
    with pytest.raises(media.MediaValidationError) as exc_info:
        media.decode_and_validate(b"not an image at all")
    message = str(exc_info.value)
    assert "unrecognized image format" in message
    assert "SVG" not in message


def test_decode_and_validate_rejects_oversized_pixel_dimensions(app_env):
    _init_db()
    big = Image.new("RGB", (6400, 6300), "red")
    big_buf = BytesIO()
    big.save(big_buf, format="PNG")

    with pytest.raises(media.MediaValidationError, match="maximum allowed pixel dimensions"):
        media.decode_and_validate(big_buf.getvalue())

    small = Image.new("RGB", (100, 100), "red")
    small_buf = BytesIO()
    small.save(small_buf, format="PNG")
    assert media.decode_and_validate(small_buf.getvalue()) is not None


def test_decode_and_validate_rejects_decompression_bomb_warning_at_load(app_env, monkeypatch):
    """DecompressionBombWarning at img.load() time is caught locally.

    Pillow warns (rather than erroring) when the decoded pixel count lands
    between 1x and 2x MAX_IMAGE_PIXELS. The global warnings filter can be
    reset or overridden by tooling, so decode_and_validate() must detect this
    via its own local warnings context.
    """
    _init_db()

    class FakeImage:
        size = (100, 100)
        mode = "RGB"

        def load(self):
            warnings.warn("decompression bomb", Image.DecompressionBombWarning)
            return self

    def fake_open(*args, **kwargs):
        return FakeImage()

    monkeypatch.setattr(media.Image, "open", fake_open)

    # Simulate the module-level filter having been reset/overridden.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(media.MediaValidationError, match="maximum allowed pixel dimensions"):
            media.decode_and_validate(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)


def test_decode_and_validate_animated_gif_stores_only_frame_0(app_env):
    _init_db()
    frame0 = Image.new("RGB", (50, 50), (255, 0, 0)).convert("P")
    frame1 = Image.new("RGB", (50, 50), (0, 0, 255)).convert("P")

    buf = BytesIO()
    frame0.save(
        buf,
        format="GIF",
        save_all=True,
        append_images=[frame1],
        duration=100,
        loop=0,
    )
    data = buf.getvalue()

    decoded = media.decode_and_validate(data)
    media_id = media.store_full_image(decoded)

    row = db.get_media(media_id)
    stored = Image.open(db.MEDIA_DIR / row["path"])
    r, g, b = stored.getpixel((25, 25))
    assert r > 200 and g < 50 and b < 50


def test_store_image_pair_returns_expected_dict_shape(app_env):
    _init_db()
    _, data = _make_image_bytes(size=(200, 200), fmt="PNG")
    img = media.decode_and_validate(data)
    result = media.store_image_pair(img)

    assert set(result.keys()) == {"media_id", "thumb_media_id", "thumb_w", "thumb_h"}
    assert isinstance(result["media_id"], str)
    assert isinstance(result["thumb_media_id"], str)
    assert isinstance(result["thumb_w"], int)
    assert isinstance(result["thumb_h"], int)
