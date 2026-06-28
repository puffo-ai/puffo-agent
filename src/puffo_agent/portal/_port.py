"""Bind a TCPSite with forward-scan fallback on port conflict."""

from __future__ import annotations

from typing import Tuple

from aiohttp import web


async def bind_tcp_with_fallback(
    runner: web.AppRunner,
    *,
    host: str,
    port: int,
    fallback_start: int | None = None,
    max_attempts: int = 100,
) -> Tuple[web.TCPSite, int]:
    """Try ``port``; on OSError, scan from ``fallback_start``
    (default ``port + 1``) up to ``max_attempts`` total. Returns
    ``(site, bound_port)``; re-raises the last OSError if exhausted.

    OSError catches both POSIX (EADDRINUSE/EACCES) and Windows
    (WSAEADDRINUSE/WSAEACCES) shapes.
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
