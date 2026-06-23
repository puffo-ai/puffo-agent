"""FastMCP lifespan hook closes the MCP subprocess's aiohttp clients
on teardown (PUF-323).

Without the lifespan, ``FastMCP.run()`` exits the stdio loop with the
DataClient + PuffoRpcClient sessions still open; Python's gc emits the
operator-confusing ``Unclosed client session`` warning during process
shutdown. These tests pin the contract:

  1. Successful teardown closes every adapter that was wired.
  2. A ``None`` ``rpc_client`` (PUFFO_RPC_URL unset) is tolerated.
  3. A close that raises doesn't strand later closes.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp._lifespan import make_lifespan


class _CloseSpy:
    """Stub adapter with an awaitable ``close()`` that records its call
    and (optionally) raises. Mirrors the surface
    ``make_lifespan`` consumes from DataClient / PuffoRpcClient /
    PuffoCoreHttpClient — they all expose ``async def close()``."""

    def __init__(self, raise_on_close: bool = False) -> None:
        self.close_count = 0
        self.raise_on_close = raise_on_close

    async def close(self) -> None:
        self.close_count += 1
        if self.raise_on_close:
            raise RuntimeError("simulated adapter teardown failure")


@pytest.mark.asyncio
async def test_lifespan_closes_each_wired_adapter():
    """Happy path: every adapter's ``close()`` is awaited on exit."""
    data = _CloseSpy()
    rpc = _CloseSpy()
    http = _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    async with lifespan(_app=None) as _:
        # Simulate the MCP server's serve loop body — nothing to do
        # for the teardown test besides existing inside the context.
        pass

    assert data.close_count == 1
    assert rpc.close_count == 1
    assert http.close_count == 1


@pytest.mark.asyncio
async def test_lifespan_tolerates_none_rpc_client():
    """PUFFO_RPC_URL unset → ``rpc_client=None`` in build_server. The
    lifespan must NOT call ``close()`` on it (would AttributeError)."""
    data = _CloseSpy()
    http = _CloseSpy()

    lifespan = make_lifespan(data, None, http)
    async with lifespan(_app=None) as _:
        pass

    assert data.close_count == 1
    assert http.close_count == 1


@pytest.mark.asyncio
async def test_lifespan_close_failure_does_not_strand_later_adapters(
    caplog,
):
    """The whole point of the lifespan is to clean up the leak; one
    adapter blowing up at teardown can't prevent the OTHERS from
    closing (otherwise we're back to the pre-PUF-323 leak for them)."""
    data = _CloseSpy(raise_on_close=True)
    rpc = _CloseSpy()
    http = _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    with caplog.at_level(logging.ERROR, logger="puffo_agent.mcp._lifespan"):
        async with lifespan(_app=None) as _:
            pass

    # First adapter raised but its close() ran (count==1).
    assert data.close_count == 1
    # Later adapters still got closed.
    assert rpc.close_count == 1
    assert http.close_count == 1
    # The failure surfaced as an ERROR-level log so the operator can
    # tell what went wrong — but didn't propagate out of the lifespan.
    assert any(
        "DataClient.close()" in rec.message
        and "raised" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_close_failure_on_middle_adapter_still_closes_last(
    caplog,
):
    """Belt-and-suspenders: the order-iteration logic continues
    iterating past an exception, not just the first one."""
    data = _CloseSpy()
    rpc = _CloseSpy(raise_on_close=True)
    http = _CloseSpy()

    lifespan = make_lifespan(data, rpc, http)
    with caplog.at_level(logging.ERROR, logger="puffo_agent.mcp._lifespan"):
        async with lifespan(_app=None) as _:
            pass

    assert data.close_count == 1
    assert rpc.close_count == 1
    assert http.close_count == 1
    assert any(
        "PuffoRpcClient.close()" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_yields_under_async_with():
    """Defensive — the lifespan needs to be a real async context
    manager so FastMCP can wrap its serve loop. Failing this would
    mean build_server's ``lifespan=`` arg is wired wrong."""
    data = _CloseSpy()
    http = _CloseSpy()

    lifespan = make_lifespan(data, None, http)
    # ``async with`` is the only way FastMCP enters/exits the lifespan;
    # if this raises ``TypeError: object ... not an async context
    # manager``, the wiring is broken.
    async with lifespan(_app=None) as ctx:
        # The lifespan yields ``None`` — handlers don't need a shared
        # context object since they already hold refs to the adapters
        # through PuffoCoreToolsConfig.
        assert ctx is None
