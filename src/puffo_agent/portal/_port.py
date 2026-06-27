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
    max_attempts: int = 100,
) -> Tuple[web.TCPSite, int]:
    """Try ``host:port``; on OSError, scan forward up to ``max_attempts``
    consecutive ports. Returns ``(site, bound_port)`` of the first that
    binds. Raises ``OSError`` (the most recent failure) if exhausted.

    ``OSError`` covers POSIX ``EADDRINUSE`` / ``EACCES`` and Windows
    ``WSAEADDRINUSE`` / ``WSAEACCES`` — all surface as the same shape.
    """
    last_exc: OSError | None = None
    for attempt in range(max_attempts):
        candidate = port + attempt
        site = web.TCPSite(runner, host=host, port=candidate)
        try:
            await site.start()
        except OSError as exc:
            last_exc = exc
            continue
        return site, candidate
    assert last_exc is not None
    raise last_exc
