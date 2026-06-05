"""MCP-side client for the daemon's ``rpc_service``.

The two host-touching MCP tools — ``install_host_mcp`` and
``sync_host_mcp`` — are thin wrappers around the routes this client
hits. All the actual work (read+write operator's ``~/.claude.json``,
catalog fetch, DM send) happens daemon-side in
``portal.host_mcp_handler``.

Why a daemon round-trip even for cli-local: keeps both runtimes on
one path so the install/sync logic doesn't fork by ``runtime_kind``,
and keeps operator's ``~/.claude.json`` as a single-writer file
(the daemon) regardless of how many concurrent agents call install.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class PuffoRpcClient:
    """Async client for the daemon's loopback RPC service.

    Construction takes the base URL (``http://127.0.0.1:63385`` for
    cli-local, ``http://host.docker.internal:63385`` for cli-docker)
    + the agent_id; the worker injects both via env. Connection
    failures + non-2xx responses are surfaced as ``RuntimeError`` so
    the MCP tool wrapper can pass the message through to the agent
    as a tool error string."""

    def __init__(self, base_url: str, agent_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _post(self, route: str, body: dict[str, Any]) -> str:
        """POST + return the ``message`` field from the response body.
        Raises on transport failure or any non-2xx response, with the
        daemon's error field if present."""
        path = (
            f"/v1/rpc/{urllib.parse.quote(self.agent_id, safe='')}/"
            f"{route.lstrip('/')}"
        )
        url = f"{self.base_url}{path}"
        session = await self._get_session()
        try:
            async with session.post(url, json=body) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    raise RuntimeError(
                        f"rpc {route} returned non-JSON body "
                        f"(status {resp.status}): {text[:500]}"
                    )
                if resp.status >= 400:
                    err = (
                        data.get("error")
                        if isinstance(data, dict) else None
                    )
                    raise RuntimeError(
                        err or f"rpc {route} failed with status {resp.status}"
                    )
                msg = (
                    data.get("message") if isinstance(data, dict) else None
                )
                if not isinstance(msg, str):
                    raise RuntimeError(
                        f"rpc {route} returned a JSON object without a "
                        f"`message` string field"
                    )
                return msg
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"rpc {route} transport error: {exc}"
            ) from exc

    async def install_mcp(
        self,
        *,
        name: str,
        template_id: str = "",
        spec: Optional[dict[str, Any]] = None,
    ) -> str:
        return await self._post(
            "install-mcp",
            {"name": name, "template_id": template_id, "spec": spec},
        )

    async def sync_mcp(self, *, template_id: str) -> str:
        return await self._post(
            "sync-mcp", {"template_id": template_id},
        )
