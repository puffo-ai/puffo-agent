"""Download, decrypt, normalize, and store inbound attachment files."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..crypto import attachments as attachment_crypto
from ..crypto.attachments import AttachmentMeta
from ..crypto.http_client import HttpError, PuffoCoreHttpClient
from ..limits import (
    MAX_INBOUND_ATTACHMENTS,
    MAX_INBOUND_ATTACHMENT_BYTES,
    MAX_INBOUND_ATTACHMENT_TOTAL_BYTES,
    MAX_INBOUND_IMAGE_PIXELS,
)

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE_EDGE_PX = 1568
_HIGH_RES_IMAGE_EDGE_PX = 2576
_HIGH_RES_MODEL_MARKERS = ("opus-4-7", "opus-4-8")
MAX_INBOUND_ATTACHMENT_CIPHERTEXT_BYTES = (
    MAX_INBOUND_ATTACHMENT_BYTES + 64 * 1024
)

BlobFetcher = Callable[[PuffoCoreHttpClient, str], Awaitable[bytes | None]]
ImageScaler = Callable[[Path, Path | None, int], bool]
Log = logging.Logger | logging.LoggerAdapter


async def fetch_blob_with_retry(
    http: PuffoCoreHttpClient,
    blob_id: str,
) -> bytes | None:
    """Fetch a blob, tolerating the short WS-to-row visibility race."""
    delays = (0, 5.0, 5.0, 5.0)
    last: Exception | None = None
    for delay in delays:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await http.get_bytes(
                f"/blobs/{blob_id}",
                max_bytes=MAX_INBOUND_ATTACHMENT_CIPHERTEXT_BYTES,
            )
        except HttpError as exc:
            last = exc
            if exc.status != 404:
                logger.warning("attachment download failed (%s): %s", blob_id, exc)
                return None
        except Exception as exc:
            last = exc
            logger.warning("attachment download failed (%s): %s", blob_id, exc)
            return None
    logger.warning(
        "attachment download still 404 after retries (%s): %s",
        blob_id,
        last,
    )
    return None


def strip_multipart_wrapper(data: bytes) -> bytes:
    """Return the largest body when legacy ciphertext contains form-data."""
    if not data.startswith(b"--"):
        return data
    newline = data.find(b"\r\n")
    if newline == -1 or newline > 256:
        return data
    boundary = data[2:newline]
    if not boundary or any(char in boundary for char in (b"\r", b"\n")):
        return data

    separator = b"--" + boundary
    candidates = [
        part
        for part in data.split(separator)[1:]
        if part and not part.startswith(b"--")
    ]
    best: bytes | None = None
    for part in candidates:
        if part.startswith(b"\r\n"):
            part = part[2:]
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        body = part[header_end + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        if best is None or len(body) > len(best):
            best = body
    return data if best is None else best


def max_image_edge_px(model: str | None) -> int:
    """Return the model's native vision long-edge limit."""
    normalized = (model or "").lower()
    if any(marker in normalized for marker in _HIGH_RES_MODEL_MARKERS):
        return _HIGH_RES_IMAGE_EDGE_PX
    return _DEFAULT_IMAGE_EDGE_PX


def downscale_oversized_image(
    path: Path,
    original_path: Path | None = None,
    max_edge_px: int = _DEFAULT_IMAGE_EDGE_PX,
) -> bool:
    """Resize an oversized image in place while optionally preserving it."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "Pillow missing — inbound images aren't dimension-checked; "
            "a many-image request can then reject an oversized one "
            "(pip install pillow)",
        )
        return False
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height > MAX_INBOUND_IMAGE_PIXELS:
                logger.warning(
                    "inbound image %s exceeds the %d-pixel safety cap",
                    path,
                    MAX_INBOUND_IMAGE_PIXELS,
                )
                return False
            longest = max(width, height)
            if longest <= max_edge_px:
                return False
            image.load()
            _preserve_original(path, original_path)
            scale = max_edge_px / longest
            resized_size = (
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            )
            image.resize(resized_size, Image.LANCZOS).save(
                path,
                format=image.format or "PNG",
            )
        logger.info(
            "downscaled inbound image %s: %dx%d -> %dx%d (cap %dpx)",
            getattr(path, "name", path),
            width,
            height,
            resized_size[0],
            resized_size[1],
            max_edge_px,
        )
        return True
    except Exception as exc:
        logger.warning("could not dimension-check image %s: %s", path, exc)
        return False


def _preserve_original(path: Path, original_path: Path | None) -> None:
    if original_path is None:
        return
    import shutil

    try:
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, original_path)
    except Exception as exc:
        logger.warning("could not preserve original image %s: %s", original_path, exc)


def is_safe_path_component(value: Any) -> bool:
    """Whether ``value`` may be used as one local directory name.

    An ingress ``envelope_id`` reaches the filesystem as
    ``<workspace>/.puffo/inbox/<envelope_id>/``, so only a plain single
    path component may be used verbatim. Deliberately not a
    ``msg_<UUID>`` whitelist: every server-issued id passes, and a
    locally originated id need not carry the server's prefix.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        return False
    if "/" in value or "\\" in value or "\0" in value:
        return False
    return Path(value).name == value


def _image_exceeds_pixel_cap(path: Path) -> bool:
    """Inspect image headers without decoding a potentially huge raster."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as image:
            width, height = image.size
            return width * height > MAX_INBOUND_IMAGE_PIXELS
    except Image.DecompressionBombError:
        return True
    except Exception:
        return False


async def save_inbound_attachments(
    *,
    workspace: str,
    envelope_id: str,
    metas_raw: list[Any],
    image_edge_px: int,
    http: PuffoCoreHttpClient,
    log: Log,
    fetch_blob: BlobFetcher = fetch_blob_with_retry,
    strip_wrapper: Callable[[bytes], bytes] = strip_multipart_wrapper,
    scale_image: ImageScaler = downscale_oversized_image,
) -> list[str]:
    """Save decrypted attachments and return host paths for durable storage.

    The conversation projection converts these to workspace-relative paths
    before exposing them to a local or containerized harness.
    """
    if not workspace or not metas_raw:
        return []
    if not is_safe_path_component(envelope_id):
        log.warning(
            "attachments skipped: envelope_id is not a safe path component (%r)",
            envelope_id,
        )
        return []
    inbox = Path(workspace) / ".puffo" / "inbox" / envelope_id
    inbox.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    total_bytes = 0
    if len(metas_raw) > MAX_INBOUND_ATTACHMENTS:
        log.warning(
            "attachment count exceeds cap; processing first %d of %d",
            MAX_INBOUND_ATTACHMENTS,
            len(metas_raw),
        )
    for raw in metas_raw[:MAX_INBOUND_ATTACHMENTS]:
        saved = await _save_one_attachment(
            raw=raw,
            inbox=inbox,
            image_edge_px=image_edge_px,
            http=http,
            log=log,
            fetch_blob=fetch_blob,
            strip_wrapper=strip_wrapper,
            scale_image=scale_image,
            remaining_bytes=MAX_INBOUND_ATTACHMENT_TOTAL_BYTES - total_bytes,
        )
        if saved is not None:
            path, size = saved
            paths.append(str(path))
            total_bytes += size
    return paths


async def _save_one_attachment(
    *,
    raw: Any,
    inbox: Path,
    image_edge_px: int,
    http: PuffoCoreHttpClient,
    log: Log,
    fetch_blob: BlobFetcher,
    strip_wrapper: Callable[[bytes], bytes],
    scale_image: ImageScaler,
    remaining_bytes: int,
) -> tuple[Path, int] | None:
    if not isinstance(raw, dict):
        return None
    try:
        meta = AttachmentMeta.from_dict(raw)
    except Exception:
        log.warning("attachment meta parse failed: %r", raw)
        return None
    if (
        meta.size < 0
        or meta.size > MAX_INBOUND_ATTACHMENT_BYTES
        or meta.size > remaining_bytes
    ):
        log.warning(
            "attachment declared size exceeds inbound limit (%s/%s: %d bytes)",
            meta.blob_id,
            meta.filename,
            meta.size,
        )
        return None
    ciphertext = await fetch_blob(http, meta.blob_id)
    if ciphertext is None:
        return None
    try:
        plaintext = attachment_crypto.decrypt_attachment(ciphertext, meta)
    except Exception as exc:
        log.warning(
            "attachment decrypt failed (%s/%s): %s",
            meta.blob_id,
            meta.filename,
            exc,
        )
        return None

    plaintext = strip_wrapper(plaintext)
    plaintext_size = len(plaintext)
    if (
        plaintext_size > MAX_INBOUND_ATTACHMENT_BYTES
        or plaintext_size > remaining_bytes
    ):
        log.warning(
            "attachment plaintext exceeds inbound limit (%s/%s: %d bytes)",
            meta.blob_id,
            meta.filename,
            plaintext_size,
        )
        return None

    # Basename-reduce both the filename and the blob_id fallback — neither
    # may escape the inbox — with a final literal so the name is never
    # empty. Mirrors the bridge saver.
    target = inbox / (
        Path(meta.filename).name or Path(str(meta.blob_id)).name or "attachment"
    )
    try:
        target.write_bytes(plaintext)
    except OSError as exc:
        log.warning("attachment save failed (%s): %s", target, exc)
        return None
    normalized = await normalize_saved_image(
        target=target,
        image_edge_px=image_edge_px,
        log=log,
        scale_image=scale_image,
    )
    return (normalized, plaintext_size) if normalized is not None else None


async def normalize_saved_image(
    *,
    target: Path,
    image_edge_px: int,
    log: Log,
    scale_image: ImageScaler,
) -> Path | None:
    unsafe = await asyncio.to_thread(_image_exceeds_pixel_cap, target)
    if unsafe:
        log.warning(
            "attachment image exceeds the %d-pixel safety cap (%s)",
            MAX_INBOUND_IMAGE_PIXELS,
            target,
        )
        try:
            target.unlink()
        except OSError:
            pass
        return None
    original = target.with_name(f"{target.stem}.origin{target.suffix}")
    resized = await asyncio.to_thread(
        scale_image,
        target,
        original,
        image_edge_px,
    )
    if not resized:
        return target
    compressed = target.with_name(f"{target.stem}.compressed{target.suffix}")
    try:
        target.rename(compressed)
        return compressed
    except OSError as exc:
        log.warning("could not rename compressed image %s: %s", target, exc)
        return target
