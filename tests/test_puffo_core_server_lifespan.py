"""FastMCP lifespan closes the MCP subprocess's aiohttp clients on teardown."""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp._lifespan import make_lifespan


class _CloseSpy:
    def __init__(self, raise_on_close: bool = False) -> None:
        self.close_count = 0
        self.raise_on_close = raise_on_close

    async def close(self) -> None:
        self.close_count += 1
        if self.raise_on_close:
            raise RuntimeError("simulated adapter teardown failure")


@pytest.mark.asyncio
async def test_lifespan_closes_each_wired_adapter():
    data, rpc, http = _CloseSpy(), _CloseSpy(), _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    async with lifespan(_app=None):
        pass

    assert data.close_count == 1
    assert rpc.close_count == 1
    assert http.close_count == 1


@pytest.mark.asyncio
async def test_lifespan_tolerates_none_rpc_client():
    """PUFFO_RPC_URL unset → rpc_client=None; must not AttributeError."""
    data, http = _CloseSpy(), _CloseSpy()

    lifespan = make_lifespan(data, None, http)
    async with lifespan(_app=None):
        pass

    assert data.close_count == 1
    assert http.close_count == 1


@pytest.mark.asyncio
async def test_lifespan_close_failure_does_not_strand_later_adapters(caplog):
    """First-adapter raise must not skip the rest."""
    data = _CloseSpy(raise_on_close=True)
    rpc, http = _CloseSpy(), _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    with caplog.at_level(logging.ERROR, logger="puffo_agent.mcp._lifespan"):
        async with lifespan(_app=None):
            pass

    assert data.close_count == 1
    assert rpc.close_count == 1
    assert http.close_count == 1
    assert any(
        "DataClient.close()" in rec.message and "raised" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_close_failure_on_middle_adapter_still_closes_last(caplog):
    """Iteration continues past every exception, not just the first."""
    data = _CloseSpy()
    rpc = _CloseSpy(raise_on_close=True)
    http = _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    with caplog.at_level(logging.ERROR, logger="puffo_agent.mcp._lifespan"):
        async with lifespan(_app=None):
            pass

    assert data.close_count == 1
    assert rpc.close_count == 1
    assert http.close_count == 1
    assert any("PuffoRpcClient.close()" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_lifespan_yields_under_async_with():
    """async-context-manager wiring — FastMCP needs `async with` to work."""
    data, http = _CloseSpy(), _CloseSpy()

    lifespan = make_lifespan(data, None, http)
    async with lifespan(_app=None) as ctx:
        assert ctx is None
