"""FastMCP lifespan hook that closes the MCP subprocess's
aiohttp.ClientSession holders on teardown.

In its own module so tests can import ``make_lifespan`` without
pulling in ``mcp.server.fastmcp`` (the SDK is optional in dev).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class _AsyncCloseable(Protocol):
    async def close(self) -> None: ...


def make_lifespan(
    data: _AsyncCloseable,
    rpc_client: _AsyncCloseable | None,
    http: _AsyncCloseable,
    startup: Callable[[], Awaitable[None]] | None = None,
):
    """Async context manager FastMCP wraps around its serve loop;
    closes every adapter session while the loop is still alive.
    ``startup`` (optional) runs as a background task so a slow or
    failing hook can never delay or break tool serving."""

    @asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        startup_task: asyncio.Task | None = None
        if startup is not None:
            startup_task = asyncio.ensure_future(startup())
        try:
            yield
        finally:
            if startup_task is not None and not startup_task.done():
                startup_task.cancel()
            # Per-adapter try so one close() raising can't strand the rest.
            for label, closer in (
                ("DataClient", data.close),
                ("PuffoRpcClient", rpc_client.close if rpc_client else None),
                ("PuffoCoreHttpClient", http.close),
            ):
                if closer is None:
                    continue
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "%s.close() raised during MCP teardown", label,
                    )

    return _lifespan
