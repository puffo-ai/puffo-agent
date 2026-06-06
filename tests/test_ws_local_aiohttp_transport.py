"""aiohttp ``WebSocketResponse`` → ``Transport`` adapter.

Text frames pass through; close/error/binary read as end-of-stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import WSMsgType

from puffo_agent.portal.ws_local.aiohttp_transport import AiohttpTransport


@dataclass
class _Msg:
    type: Any
    data: str = ""


class FakeWs:
    def __init__(self, inbound) -> None:
        self._inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, s: str) -> None:
        self.sent.append(s)

    async def receive(self):
        return self._inbound.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_send_delegates_to_send_str():
    ws = FakeWs([])
    await AiohttpTransport(ws).send("hello")
    assert ws.sent == ["hello"]


@pytest.mark.asyncio
async def test_recv_returns_text_payload():
    ws = FakeWs([_Msg(WSMsgType.TEXT, "frame")])
    assert await AiohttpTransport(ws).recv() == "frame"


@pytest.mark.parametrize("mtype", [WSMsgType.CLOSE, WSMsgType.ERROR, WSMsgType.BINARY])
@pytest.mark.asyncio
async def test_recv_non_text_reads_as_eof(mtype):
    ws = FakeWs([_Msg(mtype)])
    assert await AiohttpTransport(ws).recv() is None


@pytest.mark.asyncio
async def test_close_delegates():
    ws = FakeWs([])
    await AiohttpTransport(ws).close()
    assert ws.closed
