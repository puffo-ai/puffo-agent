"""Bind a TCPSite with forward-scan fallback on port conflict."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Tuple

from aiohttp import web

from ..tasks import spawn

logger = logging.getLogger(__name__)

_LISTENER_PROBE_INTERVAL_SECONDS = 5.0
_LISTENER_PROBE_TIMEOUT_SECONDS = 2.0
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


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


class ListenerRunner(web.AppRunner):
    """AppRunner that rebinds its exact port when the listener dies.

    Windows' proactor loop closes a listening socket for good when one
    AcceptEx fails (WinError 64 when a connecting client is killed
    mid-handshake). Agents pin the port in their MCP env, so the rebind
    must never fall back to another port.
    """

    def __init__(self, app: web.Application, **kwargs) -> None:
        super().__init__(app, **kwargs)
        self._guard: asyncio.Future | None = None
        self._address: tuple[str, int] | None = None
        self._accepting_since: float | None = None

    def keep_listening(self, *, host: str, port: int, name: str) -> None:
        self._address = (host, port)
        self._accepting_since = time.monotonic()
        self._guard = spawn(
            self._keep_listening(host=host, port=port, name=name),
            name=f"{name}.keep_listening",
        )

    async def cleanup(self) -> None:
        guard, self._guard = self._guard, None
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        await super().cleanup()

    async def accepting_since(self) -> float | None:
        """Monotonic time the listener last (re)started accepting, or
        ``None`` while it is down. Checked live: the guard only notices
        a dead listener on its next probe."""
        if self._address is None or not await _accepts(*self._address):
            return None
        return self._accepting_since

    async def _keep_listening(self, *, host: str, port: int, name: str) -> None:
        while True:
            await asyncio.sleep(_LISTENER_PROBE_INTERVAL_SECONDS)
            if await _accepts(host, port):
                continue
            self._accepting_since = None
            logger.warning(
                "%s: listener on %s:%d stopped accepting; rebinding",
                name, host, port,
            )
            for site in self.sites:
                await site.stop()
            try:
                await web.TCPSite(self, host=host, port=port).start()
            except OSError as exc:
                logger.warning(
                    "%s: rebind %s:%d failed (%s); retrying",
                    name, host, port, exc,
                )
                continue
            self._accepting_since = time.monotonic()
            logger.warning("%s: listener rebound on %s:%d", name, host, port)


async def _accepts(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in _WILDCARD_HOSTS else host
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(probe_host, port),
            timeout=_LISTENER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True
