"""FastMCP lifespan hook that closes the puffo-core MCP subprocess's
aiohttp.ClientSession holders on teardown (PUF-323).

Lives in its own module so the unit tests can import the cleanup
function without also pulling in ``mcp.server.fastmcp`` (the ``mcp``
SDK is optional in dev environments — exercising the lifespan
contract through a real FastMCP would gate the test on having the
SDK installed, which the rest of the daemon test suite already
intentionally avoids).
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
    """Build the async context manager FastMCP runs around its serve
    loop. Yields ``None`` (tool handlers already hold references to
    ``data`` / ``rpc`` / ``http`` via ``PuffoCoreToolsConfig`` — no
    shared lifespan context object needed); the value is the
    ``finally`` block that closes every adapter's aiohttp session
    while the event loop is still alive."""

    @asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Each close() is wrapped so a failure to close one
            # adapter doesn't strand the others. The unclosed-warning
            # we're fixing fires from any one of them; bailing on
            # the first exception would leave the rest leaked.
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
                        "PUF-323: %s.close() raised during MCP "
                        "subprocess teardown; other adapters will "
                        "still be closed.",
                        label,
                    )

    return _lifespan
