"""Thin metadata HTTP client for the cloud runtime.

Read-only GETs against puffo-server's metadata routes — spaces,
channels, members, identity profiles. ``aiohttp`` only. Holds NO key
material and performs NO subkey signing: this is NOT the fat agent's
signing HTTP client. Auth is a single optional ``x-sandbox-token``
header — sent when a token is configured (explicit arg or
``PUFFO_SANDBOX_TOKEN``), omitted otherwise so production E2B sandboxes
can rely on network-rule token injection at the edge.

Stage A delivers + unit-tests this client; wiring it into the runner's
live tool surface (profiles/members tools, possibly replacing the WS
``list_spaces``) is Stage B."""

from __future__ import annotations

import os
from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class CloudMetadataError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class CloudMetadataClient:
    """Read-only metadata client. One ``aiohttp`` session, reused."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        # Explicit arg wins; otherwise fall back to the env var that
        # the sandbox provisioner sets. ``None`` means "send no token"
        # (prod network-rule injection adds it at the edge).
        self._token = token if token is not None else os.environ.get(
            "PUFFO_SANDBOX_TOKEN",
        )
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"x-sandbox-token": self._token}
        return {}

    async def _get(self, path: str) -> Any:
        sess = await self._ensure_session()
        url = f"{self.base_url}{path}"
        async with sess.get(url, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise CloudMetadataError(resp.status, text)
            return await resp.json() if text else None

    async def list_spaces(self) -> Any:
        """``GET /spaces`` — spaces the agent is a member of."""
        return await self._get("/spaces")

    async def list_channels(self, space_id: str) -> Any:
        """``GET /spaces/{space_id}/channels``."""
        return await self._get(f"/spaces/{space_id}/channels")

    async def list_members(self, space_id: str) -> Any:
        """``GET /spaces/{space_id}/members``."""
        return await self._get(f"/spaces/{space_id}/members")

    async def list_profiles(self) -> Any:
        """``GET /identities/profiles`` — identity profile directory."""
        return await self._get("/identities/profiles")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
