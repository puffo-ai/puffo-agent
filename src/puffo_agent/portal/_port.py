"""Bind a TCPSite with forward-scan fallback on port conflict.

Caller owns the runner lifecycle and (optionally) mutates its config
with the resolved port so downstream readers see the bound value.
"""

from __future__ import annotations

import logging
from typing import Tuple

from aiohttp import web

logger = logging.getLogger(__name__)


async def bind_tcp_with_fallback(
    runner: web.AppRunner,
    *,
    host: str,
    port: int,
    fallback_start: int | None = None,
    max_attempts: int = 100,
) -> Tuple[web.TCPSite, int]:
    """Try ``host:port``; on OSError, scan forward from
    ``fallback_start`` (default ``port + 1``) up to ``max_attempts``
    total ports. Returns ``(site, bound_port)`` of the first that
    binds. Raises ``OSError`` (the most recent failure) if exhausted.

    ``fallback_start`` lets the caller jump past reserved ports the
    primary's scan would otherwise collide with (e.g. the pinned
    bridge service).

    ``OSError`` covers POSIX ``EADDRINUSE`` / ``EACCES`` and Windows
    ``WSAEADDRINUSE`` / ``WSAEACCES`` — all surface as the same shape.
    """
    scan_from = fallback_start if fallback_start is not None else port + 1
    last_exc: OSError | None = None
    for attempt in range(max_attempts):
        candidate = port if attempt == 0 else scan_from + (attempt - 1)
        site = web.TCPSite(runner, host=host, port=candidate)
        try:
            await site.start()
        except OSError as exc:
            last_exc = exc
            continue
        return site, candidate
    assert last_exc is not None
    raise last_exc
