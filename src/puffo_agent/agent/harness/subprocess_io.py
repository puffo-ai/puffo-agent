"""Shared bounded subprocess stream handling for CLI harness Drivers."""

from __future__ import annotations

from typing import Any


async def drain_subprocess_stream(stream: Any) -> None:
    """Continuously discard a child stream so OS pipe backpressure cannot stall it."""
    if stream is None:
        return
    while await stream.read(64 * 1024):
        pass


async def drain_subprocess_stream_keeping_tail(
    stream: Any, *, max_bytes: int = 8192
) -> bytes:
    """Like ``drain_subprocess_stream``, but return the last ``max_bytes`` seen.

    Diagnostic use only (e.g. logging why a CLI child died) — the caller
    still owns backpressure relief, we just remember the tail in passing.
    """
    if stream is None:
        return b""
    tail = b""
    while chunk := await stream.read(64 * 1024):
        tail = (tail + chunk)[-max_bytes:]
    return tail
