"""Auto-port-fallback helper shared by the daemon's loopback HTTP
services. PUF-327.

The data + RPC services historically pinned 63386 + 63385. If
something else holds the port at startup — another puffo-agent in
a clean-up window, an unrelated process, Windows' ``WSAEACCES``
permission denial (`winerror 10013`) on a port already in the OS's
excluded-range list — the bind failed and the operator had to
hand-edit ``daemon.yml`` to pick alternates. The fallback path
below scans forward by one port at a time within a small window
(default 100), reports the resolved port, and lets the caller
mutate its config so the MCP-subprocess env-var passthrough sees
the actual port without further plumbing.

This module deliberately knows nothing about aiohttp specifics —
the helper takes a pre-built ``runner`` and host + initial port,
constructs + starts a ``TCPSite`` for each attempt, and returns
the running site plus the resolved port. The caller owns
``runner`` lifecycle.
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
    """Try to bind ``runner`` to ``host:port``; on ``OSError`` scan
    forward up to ``max_attempts`` consecutive ports and return the
    first one that binds.

    Returns ``(site, bound_port)``. Caller is responsible for
    keeping ``site`` alive (it's already ``.start()``-ed). Raises
    ``OSError`` if the entire window is exhausted — the caller
    already handles bind-failure as non-fatal degradation, so the
    exception bubbles up to the existing log-and-return-None path.

    ``OSError`` is the right catch shape cross-platform:
    - POSIX: ``EADDRINUSE`` (port taken), ``EACCES`` (privileged
      port without CAP_NET_BIND_SERVICE)
    - Windows: ``WSAEADDRINUSE`` (10048) and ``WSAEACCES`` (10013,
      the `winerror 10013` symptom Jeremy_S surfaced in FB-338)
    All four surface as ``OSError`` subclasses in Python, so a
    single ``except OSError`` covers the lot.
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
    # Window exhausted — re-raise the most recent failure so the
    # caller's existing ``except OSError`` path logs + returns
    # None. The window is generous enough (100) that exhaustion is
    # genuinely surprising; the log message in the caller is the
    # right surface for operator attention.
    assert last_exc is not None
    raise last_exc
