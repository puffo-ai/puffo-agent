"""The ``list_credentials`` tool, handler and RPC route.

Metadata only: the one property worth protecting here is that a secret can
never travel this path, whatever the server puts in the listing.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from aiohttp import web
from mcp.server.fastmcp import FastMCP

from puffo_agent.mcp.config import PUFFO_CORE_TOOL_NAMES
from puffo_agent.mcp.core_host_mcp_tools import register_host_mcp_tools
from puffo_agent.portal import host_mcp_handler


class _Wallet:
    """Stands in for ``AgentCredentials``; ``held_all`` is all this path uses."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def held_all(self):
        self.calls += 1
        return self.rows


def _ctx(credentials):
    return NS(credentials=credentials)


ROW = {"type": "PUFFO_GOOGLE_OAUTH_v1", "index": 0, "version": 3, "alias": "work@x.test"}


# ── handler ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_reports_the_metadata_of_everything_held():
    wallet = _Wallet([ROW, {"type": "CUSTOMIZED", "index": 1, "version": 1, "alias": "GH"}])
    result = await host_mcp_handler.list_credentials(_ctx(wallet))
    assert result == {"credentials": [
        {"type": "PUFFO_GOOGLE_OAUTH_v1", "index": 0, "version": 3, "alias": "work@x.test"},
        {"type": "CUSTOMIZED", "index": 1, "version": 1, "alias": "GH"},
    ]}
    assert wallet.calls == 1


@pytest.mark.asyncio
async def test_a_value_in_the_listing_is_not_passed_on():
    """The projection is the guard: extra fields are dropped, not forwarded."""
    leaky = dict(ROW, blob="c2VjcmV0", value="hunter2", access_token="ya29.secret")
    result = await host_mcp_handler.list_credentials(_ctx(_Wallet([leaky])))
    assert result["credentials"] == [ROW]
    assert "hunter2" not in repr(result) and "ya29.secret" not in repr(result)


@pytest.mark.asyncio
async def test_holding_nothing_is_an_empty_list_not_an_error():
    assert await host_mcp_handler.list_credentials(_ctx(_Wallet([]))) == {"credentials": []}


@pytest.mark.asyncio
async def test_an_agent_that_cannot_hold_credentials_says_so():
    with pytest.raises(RuntimeError, match="cannot hold credentials"):
        await host_mcp_handler.list_credentials(_ctx(None))


# ── tool ─────────────────────────────────────────────────────────────


class _StubRpc:
    def __init__(self):
        self.calls = 0

    async def list_credentials(self):
        self.calls += 1
        return {"credentials": [ROW]}


def _tools(rpc):
    mcp = FastMCP("test")
    register_host_mcp_tools(mcp, NS(rpc_client=rpc, workspace=None))
    return mcp._tool_manager._tools


@pytest.mark.asyncio
async def test_the_tool_forwards_to_the_rpc_client():
    rpc = _StubRpc()
    assert await _tools(rpc)["list_credentials"].fn() == {"credentials": [ROW]}
    assert rpc.calls == 1


@pytest.mark.asyncio
async def test_the_tool_without_rpc_gives_guidance():
    with pytest.raises(RuntimeError, match="PUFFO_RPC_URL"):
        await _tools(None)["list_credentials"].fn()


def test_the_tool_is_in_the_core_tool_list():
    # Absent here, the CLI refuses the call however well the rest is wired.
    assert "list_credentials" in PUFFO_CORE_TOOL_NAMES


def test_the_docstring_promises_metadata_only():
    doc = (_tools(_StubRpc())["list_credentials"].description or "").lower()
    assert "metadata only" in doc and "never returned here" in doc


# ── rpc client ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_rpc_client_posts_the_route_with_no_arguments(monkeypatch):
    from puffo_agent.mcp._host_mcp import PuffoRpcClient

    posted = []

    async def _post_object(self, route, body):
        posted.append((route, body))
        return {"credentials": []}

    monkeypatch.setattr(PuffoRpcClient, "_post_object", _post_object)
    client = PuffoRpcClient("http://127.0.0.1:1", "a1")
    assert await client.list_credentials() == {"credentials": []}
    assert posted == [("list-credentials", {})]


# ── route ────────────────────────────────────────────────────────────


def _route_app():
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post(
        "/v1/rpc/{agent_id}/list-credentials", rpc_service.list_credentials_route
    )
    return app, rpc_service


async def _through_route(resolver, payload={}, raw=None):
    from aiohttp.test_utils import TestClient, TestServer

    app, rpc_service = _route_app()
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = resolver
    try:
        async with TestClient(TestServer(app)) as http:
            kw = {"data": raw} if raw is not None else {"json": payload}
            resp = await http.post("/v1/rpc/agent-1/list-credentials", **kw)
            return resp.status, await resp.json()
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_the_route_returns_the_listing():
    status, body = await _through_route(lambda agent_id: _ctx(_Wallet([ROW])))
    assert status == 200 and body == {"credentials": [ROW]}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,raw", [
    (None, "not json"),
    ({"type": "PUFFO_GOOGLE_OAUTH_v1"}, None),  # no filtering: say so, don't ignore it
    ([], None),
])
async def test_the_route_refuses_anything_it_does_not_recognise(payload, raw):
    status, _ = await _through_route(
        lambda agent_id: _ctx(_Wallet([ROW])), payload=payload, raw=raw
    )
    assert status == 400


@pytest.mark.asyncio
async def test_the_route_reports_no_worker_rather_than_failing_obscurely():
    status, body = await _through_route(lambda agent_id: None)
    assert status == 409 and body["code"] == "no_worker"


@pytest.mark.asyncio
async def test_the_route_turns_a_handler_refusal_into_a_4xx():
    status, body = await _through_route(lambda agent_id: _ctx(None))
    assert status == 400 and "cannot hold credentials" in body["error"]
