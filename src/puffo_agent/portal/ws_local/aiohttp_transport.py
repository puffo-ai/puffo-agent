"""Adapt an aiohttp ``WebSocketResponse`` to the session ``Transport``.

Thin: text frames pass through; anything else (close, error, binary)
reads as end-of-stream so the session tears down cleanly.
"""

from __future__ import annotations

from typing import Any, Optional

from aiohttp import WSMsgType


class AiohttpTransport:
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, raw: str) -> None:
        await self._ws.send_str(raw)

    async def recv(self) -> Optional[str]:
        msg = await self._ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        return None

    async def close(self) -> None:
        await self._ws.close()
