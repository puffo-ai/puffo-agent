"""HTTP transport for the Bridge outbound seam.

A thin aiohttp client that POSTs plaintext to the Bridge with the
session token; the Bridge does the encrypt/sign/forward to puffo-server
(the sandbox holds no keys). The wire paths here track the not-yet-locked
Bridge contract — they run in prod against the real Bridge, while unit
tests drive cli-cloud through ``StubBridgeClient`` instead.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from .client import BridgeConfig

logger = logging.getLogger(__name__)


class HttpBridgeOutbound:
    """``BridgeOutbound`` over HTTP. One session, bearer-authed."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.config.session_token}"}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def _url(self, path: str) -> str:
        return self.config.bridge_url.rstrip("/") + path

    async def send_message(
        self,
        *,
        channel: str,
        text: str,
        is_visible_to_human: bool,
        root_id: str = "",
    ) -> dict:
        sess = await self._sess()
        payload = {
            "channel": channel,
            "text": text,
            "is_visible_to_human": is_visible_to_human,
            "root_id": root_id,
        }
        async with sess.post(self._url("/v1/outbound/send_message"), json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def report_status(self, status: dict) -> None:
        sess = await self._sess()
        async with sess.post(self._url("/v1/outbound/status"), json=status) as resp:
            resp.raise_for_status()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
