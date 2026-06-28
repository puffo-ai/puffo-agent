"""HTTP + WS client to puffo-cloud-server, authenticated by a
bearer ``session_token``."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)


_TIMEOUT = aiohttp.ClientTimeout(total=60)


class CloudHttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class CloudHttpClient:
    """``Authorization: Bearer <session_token>`` wrapper. One session
    per agent, shared across the worker's lifetime."""

    def __init__(self, base_url: str, session_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = session_token
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def post(self, path: str, body: dict) -> dict:
        sess = await self._ensure_session()
        url = f"{self.base_url}{path}"
        async with sess.post(url, json=body, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise CloudHttpError(resp.status, text)
            return json.loads(text) if text else {}

    async def get(self, path: str) -> dict:
        sess = await self._ensure_session()
        url = f"{self.base_url}{path}"
        async with sess.get(url, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise CloudHttpError(resp.status, text)
            return json.loads(text) if text else {}

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


class CloudWsClient:
    """WS connection that forwards inbound envelopes. Auth via
    ``Authorization: Bearer`` on the connect handshake (same shape
    aiohttp ws_connect accepts via ``headers=``). Exponential
    reconnect on disconnect; ``listen()`` yields raw frames."""

    def __init__(self, base_url: str, session_token: str, agent_slug: str) -> None:
        # http→ws / https→wss
        ws_base = base_url.replace("http", "ws", 1)
        self._url = f"{ws_base}/v1/ws/{agent_slug}"
        self._token = session_token
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def listen(self) -> AsyncIterator[dict]:
        """Yield each inbound JSON frame. Auto-reconnects on
        transport errors with exponential backoff up to 30s."""
        backoff = 1.0
        while not self._stop.is_set():
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            try:
                async with session.ws_connect(
                    self._url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    heartbeat=30,
                ) as ws:
                    logger.info("api-puffo: WS connected to %s", self._url)
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                yield json.loads(msg.data)
                            except json.JSONDecodeError as exc:
                                logger.warning(
                                    "api-puffo: dropped malformed WS frame: %s", exc,
                                )
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning(
                    "api-puffo: WS disconnected (%s: %s); reconnect in %.1fs",
                    type(exc).__name__, exc, backoff,
                )
            finally:
                await session.close()
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)


# ── Cloud endpoint shapes (placeholders) ─────────────────────────────


async def llm_complete(
    http: CloudHttpClient,
    *,
    api_key: str,
    provider: str,
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """``POST /v1/llm/complete`` — forward an LLM call. Cloud uses
    the provided api_key against the named provider. Response shape
    mirrors Anthropic's tool-using messages API for now (cloud is
    expected to normalise across providers)."""
    body: dict[str, Any] = {
        "api_key": api_key,
        "provider": provider,
        "model": model,
        "system_prompt": system_prompt,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    return await http.post("/v1/llm/complete", body)


async def send_message(
    http: CloudHttpClient,
    *,
    channel: str,
    text: str,
    root_id: str = "",
    is_visible_to_human: bool = True,
) -> dict:
    """``POST /v1/send_message`` — cloud handles encrypt + sign +
    post. ``channel`` is either ``@<slug>`` for DM or ``ch_<uuid>``
    for a channel."""
    body = {
        "channel": channel,
        "text": text,
        "root_id": root_id,
        "is_visible_to_human": is_visible_to_human,
    }
    return await http.post("/v1/send_message", body)
