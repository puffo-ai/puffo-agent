"""End-to-end coverage for the dual-storage attachment write path: when an
inbound image is oversized it's split into ``<stem>.compressed<ext>`` (the
in-bounds LLM-payload version, returned in the attachment list) and
``<stem>.origin<ext>`` (the full-fidelity sibling). Non-oversized
attachments keep their bare name with no spurious siblings.
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
    _DEFAULT_IMAGE_EDGE_PX,
    _HIGH_RES_IMAGE_EDGE_PX,
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


def _stub_self(workspace: Path, edge_px: int = _DEFAULT_IMAGE_EDGE_PX) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=str(workspace),
        http=object(),
        _log=logging.getLogger("puf308-test"),
        _image_edge_px=edge_px,
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


def test_oversized_image_splits_into_compressed_and_origin(tmp_path, monkeypatch):
    """An oversized inbound image lands as ``shot.compressed.png`` (downscaled,
    returned in the attachment list) plus ``shot.origin.png`` (full-res). The
    bare ``shot.png`` is renamed away, not left behind."""
    oversized = _png_bytes(4000, 1000)
    _patch_decrypt(monkeypatch, {"blob-1": oversized})

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_a", metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_a"
    compressed = inbox / "shot.compressed.png"
    origin = inbox / "shot.origin.png"
    assert compressed.exists()
    assert origin.exists()
    assert not (inbox / "shot.png").exists()
    assert origin.read_bytes() == oversized
    with Image.open(compressed) as img:
        assert max(img.size) <= _DEFAULT_IMAGE_EDGE_PX
    assert paths == [str(compressed)]


def test_origin_mtime_tracks_pre_resize_source(tmp_path, monkeypatch):
    """``shutil.copy2`` preserves the source mtime, so ``shot.origin.png``
    carries the pre-resize file's mtime."""
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

    origin = tmp_path / ".puffo" / "inbox" / "env_mtime" / "shot.origin.png"
    assert abs(origin.stat().st_mtime - pre_resize_mtime["src_mtime"]) < 1


def test_repeated_envelope_overwrites_origin_with_latest_bytes(
    tmp_path, monkeypatch,
):
    """Envelope-id reuse: a second call with the same envelope_id + filename
    but different bytes lands the latest version in ``shot.origin.png``."""
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

    _patch_decrypt(monkeypatch, {"blob-1": second})
    asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_dup",
            metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    origin = tmp_path / ".puffo" / "inbox" / "env_dup" / "shot.origin.png"
    assert origin.read_bytes() == second


def test_concurrent_oversized_writes_share_inbox(tmp_path, monkeypatch):
    """Two oversized images in one envelope each split into their own
    compressed/origin pair in the same inbox dir without collision."""
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

    inbox = tmp_path / ".puffo" / "inbox" / "env_race"
    assert (inbox / "a.compressed.png").exists()
    assert (inbox / "b.compressed.png").exists()
    assert (inbox / "a.origin.png").read_bytes() == overs_a
    assert (inbox / "b.origin.png").read_bytes() == overs_b


def test_small_image_keeps_bare_name(tmp_path, monkeypatch):
    """A small image doesn't trip the downscale, so it keeps its bare name
    with no compressed/origin split."""
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
    assert not (inbox / "thumb.compressed.png").exists()
    assert not (inbox / "thumb.origin.png").exists()
    assert paths == [str(inbox / "thumb.png")]


def test_within_high_res_cap_keeps_bare_name(tmp_path, monkeypatch):
    """At the 2576px high-res cap (Opus 4.7+), a 2000px image is in-bounds —
    no split, bare name retained."""
    img = _png_bytes(2000, 1200)
    _patch_decrypt(monkeypatch, {"blob-1": img})

    stub = _stub_self(tmp_path, edge_px=_HIGH_RES_IMAGE_EDGE_PX)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_hr", metas_raw=[_meta("blob-1", "shot.png")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_hr"
    assert (inbox / "shot.png").exists()
    assert not (inbox / "shot.origin.png").exists()
    assert paths == [str(inbox / "shot.png")]


def test_non_image_attachment_keeps_bare_name(tmp_path, monkeypatch):
    """A text attachment never hits the downscale gate; bare name, no split."""
    _patch_decrypt(monkeypatch, {"blob-1": b"hello world"})

    stub = _stub_self(tmp_path)
    paths = asyncio.run(
        PuffoCoreMessageClient._save_inbound_attachments(
            stub, envelope_id="env_c", metas_raw=[_meta("blob-1", "notes.txt")],
        )
    )

    inbox = tmp_path / ".puffo" / "inbox" / "env_c"
    assert (inbox / "notes.txt").exists()
    assert not (inbox / "notes.origin.txt").exists()
    assert paths == [str(inbox / "notes.txt")]


def test_mixed_envelope_only_oversized_splits(tmp_path, monkeypatch):
    """A single envelope with text + small image + oversized image: only the
    oversized one gets the compressed/origin split."""
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
    assert (inbox / "big.compressed.png").exists()
    assert (inbox / "big.origin.png").read_bytes() == oversized
    assert not (inbox / "thumb.origin.png").exists()
    assert not (inbox / "notes.origin.txt").exists()
    assert set(paths) == {
        str(inbox / "notes.txt"),
        str(inbox / "thumb.png"),
        str(inbox / "big.compressed.png"),
    }
