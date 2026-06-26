"""FastMCP lifespan hook that closes the MCP subprocess's
aiohttp.ClientSession holders on teardown.

In its own module so tests can import ``make_lifespan`` without
pulling in ``mcp.server.fastmcp`` (the SDK is optional in dev).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

logger = logging.getLogger(__name__)


class _AsyncCloseable(Protocol):
    async def close(self) -> None: ...


def make_lifespan(
    data: _AsyncCloseable,
    rpc_client: _AsyncCloseable | None,
    http: _AsyncCloseable,
):
    """Async context manager FastMCP wraps around its serve loop;
    closes every adapter session while the loop is still alive."""

    @asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
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
