"""Tests for ``_downscale_oversized_image`` — the prevention half of
the oversized-image session-poison fix. An inbound image whose longest
edge tops Anthropic's 2000px many-image cap, once Read into the claude
session, makes every later turn fail wholesale. We downscale the file
on disk at save time so claude-code only ever loads in-bounds images.
"""

from __future__ import annotations

from PIL import Image

from puffo_agent.agent.puffo_core_client import (
    _MAX_IMAGE_EDGE_PX,
    _downscale_oversized_image,
)


def test_oversized_landscape_image_is_downscaled(tmp_path):
    """A wide image over the cap is resized in place, longest edge
    pinned to the cap, aspect ratio preserved."""
    path = tmp_path / "wide.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)

    _downscale_oversized_image(path)

    with Image.open(path) as img:
        w, h = img.size
    assert max(w, h) <= _MAX_IMAGE_EDGE_PX
    # 4000:1000 == 4:1 — within rounding.
    assert abs(w / h - 4.0) < 0.05
    assert w == _MAX_IMAGE_EDGE_PX


def test_oversized_portrait_image_is_downscaled(tmp_path):
    """Tall image: the HEIGHT is the longest edge and gets pinned."""
    path = tmp_path / "tall.png"
    Image.new("RGB", (800, 3200), color="blue").save(path)

    _downscale_oversized_image(path)

    with Image.open(path) as img:
        w, h = img.size
    assert max(w, h) <= _MAX_IMAGE_EDGE_PX
    assert h == _MAX_IMAGE_EDGE_PX


def test_image_at_the_cap_is_untouched(tmp_path):
    """An image already within bounds is left byte-for-byte alone —
    no needless re-encode."""
    path = tmp_path / "ok.png"
    Image.new("RGB", (_MAX_IMAGE_EDGE_PX, 900), color="green").save(path)
    before = path.read_bytes()

    _downscale_oversized_image(path)

    assert path.read_bytes() == before


def test_small_jpeg_preserves_format(tmp_path):
    """Format is preserved on resize — a JPEG stays a JPEG. (Use an
    oversized JPEG so the resize+save path actually runs.)"""
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (5000, 2500), color="white").save(path, format="JPEG")

    _downscale_oversized_image(path)

    with Image.open(path) as img:
        assert img.format == "JPEG"
        assert max(img.size) <= _MAX_IMAGE_EDGE_PX


def test_non_image_file_is_left_alone(tmp_path):
    """A non-image attachment (text, pdf, …) is a no-op, no raise."""
    path = tmp_path / "notes.txt"
    path.write_text("just some text, definitely not a PNG", encoding="utf-8")
    before = path.read_bytes()

    _downscale_oversized_image(path)  # must not raise

    assert path.read_bytes() == before


def test_corrupt_image_is_left_alone(tmp_path):
    """A file with an image extension but garbage bytes: Pillow can't
    open it (claude-code's loader can't either, so it can't reach the
    API as an oversized image). Best-effort — no raise, file untouched.
    """
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # header, junk body
    before = path.read_bytes()

    _downscale_oversized_image(path)  # must not raise

    assert path.read_bytes() == before
