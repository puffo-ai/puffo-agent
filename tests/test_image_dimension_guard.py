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


# ---- original-preserve side ----------------------------------------------
# When the caller hands an ``original_path``, the pre-resize bytes are
# copied there ONLY when the resize is actually about to fire — small
# images, non-images, and unreadable files don't get a spurious copy.


def test_original_path_preserved_on_resize(tmp_path):
    """Oversized image + original_path → both files exist; original
    holds the pre-resize bytes; in-place file is downscaled."""
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)
    pre_resize = path.read_bytes()
    original = tmp_path / "original" / "big.png"

    _downscale_oversized_image(path, original)

    assert original.read_bytes() == pre_resize
    with Image.open(path) as img:
        assert max(img.size) <= _MAX_IMAGE_EDGE_PX


def test_no_original_copy_when_image_within_cap(tmp_path):
    """Small image + original_path → no original copy: downscale is a
    no-op, so the agent shouldn't get a misleading 'original/' dir."""
    path = tmp_path / "ok.png"
    Image.new("RGB", (_MAX_IMAGE_EDGE_PX, 900), color="green").save(path)
    original = tmp_path / "original" / "ok.png"

    _downscale_oversized_image(path, original)

    assert not original.exists()
    assert not original.parent.exists()


def test_no_original_copy_for_non_image(tmp_path):
    """Non-image attachment + original_path → no spurious copy. The
    Pillow open fails before we reach the resize gate."""
    path = tmp_path / "notes.txt"
    path.write_text("not an image", encoding="utf-8")
    original = tmp_path / "original" / "notes.txt"

    _downscale_oversized_image(path, original)

    assert not original.exists()


def test_no_original_copy_for_corrupt_image(tmp_path):
    """Garbage-bytes-with-PNG-extension + original_path → no copy:
    Pillow can't decode, so resize never runs."""
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    original = tmp_path / "original" / "broken.png"

    _downscale_oversized_image(path, original)

    assert not original.exists()


def test_no_original_copy_when_pillow_missing(tmp_path, monkeypatch):
    """Pillow ImportError + original_path → early-return before any
    decision; no copy, no raise."""
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)
    original = tmp_path / "original" / "big.png"

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("simulated missing Pillow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _downscale_oversized_image(path, original)

    assert not original.exists()


def test_original_path_omitted_keeps_legacy_behavior(tmp_path):
    """No ``original_path`` arg → exactly the pre-PUF-308 behavior:
    in-place resize, no sibling files. Backward-compat for any caller
    that hasn't been migrated."""
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)
    sibling_dir = tmp_path / "original"

    _downscale_oversized_image(path)

    with Image.open(path) as img:
        assert max(img.size) <= _MAX_IMAGE_EDGE_PX
    assert not sibling_dir.exists()


def test_original_path_dir_created_on_demand(tmp_path):
    """Caller passes a nested ``original_path`` whose parent dir
    doesn't exist yet → function creates it before the copy."""
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)
    pre = path.read_bytes()
    original = tmp_path / "deep" / "nested" / "original" / "big.png"

    _downscale_oversized_image(path, original)

    assert original.read_bytes() == pre


def test_resize_failure_does_not_raise_after_original_copied(
    tmp_path, monkeypatch,
):
    """If the in-place save raises AFTER the original copy lands,
    the function still swallows the exception (best-effort contract)
    and the original copy is left on disk — caller-visible behavior
    matches the pre-PUF-308 best-effort guarantee."""
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), color="red").save(path)
    original = tmp_path / "original" / "big.png"

    # Force the post-copy resize.save() to blow up.
    real_resize = Image.Image.resize

    def boom_resize(self, *args, **kwargs):
        out = real_resize(self, *args, **kwargs)

        def boom_save(*a, **kw):
            raise OSError("simulated disk full")

        out.save = boom_save
        return out

    monkeypatch.setattr(Image.Image, "resize", boom_resize)

    _downscale_oversized_image(path, original)  # must not raise

    # Original copy made it to disk before the save blew up — that's
    # fine; the agent's file-access tools can still reach it.
    assert original.exists()
