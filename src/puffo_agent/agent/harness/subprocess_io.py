"""Shared bounded subprocess stream handling for CLI harness Drivers."""

from __future__ import annotations

from typing import Any


async def drain_subprocess_stream(stream: Any) -> None:
    """Continuously discard a child stream so OS pipe backpressure cannot stall it."""
    if stream is None:
        return
    while await stream.read(64 * 1024):
        pass
