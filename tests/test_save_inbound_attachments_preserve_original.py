"""End-to-end coverage for the dual-storage attachment write path:
inbound oversized images land downscaled at
``inbox/<env>/<name>`` (LLM-payload) AND originals land at
``inbox/<env>/original/<name>`` (agent file-access). Non-oversized
attachments don't get a spurious ``original/`` sibling.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import (
    _MAX_IMAGE_EDGE_PX,
    PuffoCoreMessageClient,
)


def _meta(blob_id: str, filename: str) -> dict:
    return {
        "blob_id": blob_id,
        "filename": filename,
        "mime_type": "image/png",
        "size": 0,
        "key": "a" * 43,
        "nonce": "b" * 16,
    }


def _png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color="red").save(buf, format="PNG")
    return buf.getvalue()


def _stub_self(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=str(workspace),
        http=object(),
        _log=logging.getLogger("puf308-test"),
    )


def _patch_decrypt(monkeypatch, payloads: dict[str, bytes]) -> None:
    async def fake_fetch(http, blob_id):
        return payloads[blob_id]

    def fake_decrypt(ciphertext, meta):
        return ciphertext

    monkeypatch.setattr(pcc, "_fetch_blob_with_retry", fake_fetch)
    monkeypatch.setattr(
        "puffo_agent.crypto.attachments.decrypt_attachment", fake_decrypt,
    )


def test_oversized_image_lands_in_both_locations(tmp_path, monkeypatch):
    """Happy path: an oversized inbound image is downscaled in place
    at ``inbox/<env>/<name>`` and the original bytes are preserved
    at ``inbox/<env>/original/<name>``. Returned LLM-payload paths
    point at the downscaled file only. ``shutil.copy2`` is used so
    the original's mtime tracks the on-disk source — agent tools
    that key freshness on stat() see a coherent timestamp."""
    oversized = _png_bytes(4000, 1000)
    _patch_decrypt(monkeypatch, {"blob-1": oversized})

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_a", metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_a"
    downscaled = inbox / "shot.png"
    original = inbox / "original" / "shot.png"
    assert downscaled.exists()
    assert original.exists()
    assert original.read_bytes() == oversized
    with Image.open(downscaled) as img:
        assert max(img.size) <= _MAX_IMAGE_EDGE_PX
    assert paths == [str(downscaled)]


def test_original_mtime_tracks_pre_resize_source(tmp_path, monkeypatch):
    """``shutil.copy2`` preserves the source mtime. The original copy
    must carry the pre-resize file's mtime — any agent tool that
    keys cache freshness on stat() sees a coherent timestamp linked
    to the actual receive moment, not the resize moment."""
    import shutil as _shutil

    oversized = _png_bytes(4000, 1000)
    pre_resize_mtime: dict[str, float] = {}
    real_copy2 = _shutil.copy2

    def spying_copy2(src, dst, *args, **kwargs):
        pre_resize_mtime["src_mtime"] = Path(src).stat().st_mtime
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(_shutil, "copy2", spying_copy2)

    _patch_decrypt(monkeypatch, {"blob-1": oversized})
    stub = _stub_self(tmp_path)
    asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_mtime",
            metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    original = tmp_path / ".puffo" / "inbox" / "env_mtime" / "original" / "shot.png"
    # copy2's mtime preservation is the contract we're pinning;
    # tolerate filesystem-rounding to whole seconds across platforms.
    assert abs(original.stat().st_mtime - pre_resize_mtime["src_mtime"]) < 1


def test_repeated_envelope_overwrites_original_with_latest_bytes(
    tmp_path, monkeypatch,
):
    """Envelope-id reuse / partial-retry rewrite case: a second call
    with the same envelope_id and filename but different bytes lands
    the latest version in ``original/`` (last-write-wins semantic).
    Same envelope_id and filename can occur on daemon-restart-during-
    write or relay-side replay; users should see the most-recently-
    received bytes, not a stale first copy."""
    first = _png_bytes(4000, 1000)
    second = _png_bytes(5000, 800)
    assert first != second

    _patch_decrypt(monkeypatch, {"blob-1": first})
    stub = _stub_self(tmp_path)
    asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_dup",
            metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    # Second pass: replace the patched payload with `second`.
    _patch_decrypt(monkeypatch, {"blob-1": second})
    asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_dup",
            metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    original = tmp_path / ".puffo" / "inbox" / "env_dup" / "original" / "shot.png"
    assert original.read_bytes() == second


def test_concurrent_oversized_writes_share_original_dir(
    tmp_path, monkeypatch,
):
    """Forward-looking race: if a future refactor parallelises
    attachment writes via ``asyncio.gather`` over ``metas_raw``,
    multiple oversized images in one envelope must share the
    ``original/`` subdir without a ``FileExistsError`` collision.
    ``Path.mkdir(parents=True, exist_ok=True)`` covers the gate;
    this test pins that guarantee against future changes."""
    overs_a = _png_bytes(4000, 1000)
    overs_b = _png_bytes(3000, 1200)
    _patch_decrypt(monkeypatch, {"blob-a": overs_a, "blob-b": overs_b})
    stub = _stub_self(tmp_path)

    async def race() -> list[list[str]]:
        return await asyncio.gather(
            PuffoCoreMessageClient._save_inbound_attachments(
                stub, envelope_id="env_race",
                metas_raw=[_meta("blob-a", "a.png")],
            ),
            PuffoCoreMessageClient._save_inbound_attachments(
                stub, envelope_id="env_race",
                metas_raw=[_meta("blob-b", "b.png")],
            ),
        )

    asyncio.run(race())

    original_dir = tmp_path / ".puffo" / "inbox" / "env_race" / "original"
    assert (original_dir / "a.png").exists()
    assert (original_dir / "b.png").exists()
    assert (original_dir / "a.png").read_bytes() == overs_a
    assert (original_dir / "b.png").read_bytes() == overs_b


def test_small_image_no_original_sibling(tmp_path, monkeypatch):
    """Unhappy-of-original-preservation: a small image doesn't trip
    the downscale, so the ``original/`` subdir is never created. The
    LLM-payload path is the (untouched) original."""
    small = _png_bytes(800, 600)
    _patch_decrypt(monkeypatch, {"blob-1": small})

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_b", metas_raw=[_meta("blob-1", "thumb.png")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_b"
    assert (inbox / "thumb.png").exists()
    assert not (inbox / "original").exists()
    assert paths == [str(inbox / "thumb.png")]


def test_non_image_attachment_no_original_sibling(tmp_path, monkeypatch):
    """A text attachment never hits the downscale gate; no spurious
    ``original/`` directory."""
    _patch_decrypt(monkeypatch, {"blob-1": b"hello world"})

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_c", metas_raw=[_meta("blob-1", "notes.txt")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_c"
    assert (inbox / "notes.txt").exists()
    assert not (inbox / "original").exists()
    assert paths == [str(inbox / "notes.txt")]


def test_mixed_envelope_only_oversized_gets_original(tmp_path, monkeypatch):
    """Race-shape coverage: a single envelope with text + small image
    + oversized image. The ``original/`` subdir holds only the
    oversized image; the LLM-payload list carries one path per
    attachment."""
    oversized = _png_bytes(4000, 1000)
    small = _png_bytes(800, 600)
    _patch_decrypt(monkeypatch, {
        "blob-1": b"some text",
        "blob-2": small,
        "blob-3": oversized,
    })

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_d",
            metas_raw=[
                _meta("blob-1", "notes.txt"),
                _meta("blob-2", "thumb.png"),
                _meta("blob-3", "big.png"),
            ],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_d"
    original_dir = inbox / "original"
    assert original_dir.is_dir()
    assert (original_dir / "big.png").exists()
    assert (original_dir / "big.png").read_bytes() == oversized
    assert not (original_dir / "thumb.png").exists()
    assert not (original_dir / "notes.txt").exists()
    assert set(paths) == {
        str(inbox / "notes.txt"),
        str(inbox / "thumb.png"),
        str(inbox / "big.png"),
    }
