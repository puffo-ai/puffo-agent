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
    point at the downscaled file only."""
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
