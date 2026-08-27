"""Bounded stderr-tail capture used to diagnose an unexpected CLI exit."""
from __future__ import annotations

import pytest

from puffo_agent.agent.harness.subprocess_io import drain_subprocess_stream_keeping_tail


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


@pytest.mark.asyncio
async def test_keeping_tail_returns_empty_for_none_stream():
    assert await drain_subprocess_stream_keeping_tail(None) == b""


@pytest.mark.asyncio
async def test_keeping_tail_concatenates_short_output():
    stream = _FakeStream([b"panic: ", b"boom\n"])
    tail = await drain_subprocess_stream_keeping_tail(stream)
    assert tail == b"panic: boom\n"


@pytest.mark.asyncio
async def test_keeping_tail_bounds_to_max_bytes():
    stream = _FakeStream([b"a" * 5000, b"b" * 5000])
    tail = await drain_subprocess_stream_keeping_tail(stream, max_bytes=100)
    assert len(tail) == 100
    assert tail == b"b" * 100
