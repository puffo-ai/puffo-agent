"""Tests for ``portal.rpc_service`` — the dispatch layer between
the MCP-side ``PuffoRpcClient`` and the daemon-side
``host_mcp_handler`` functions."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import rpc_service
from puffo_agent.portal.host_mcp_handler import HostMcpContext


def _stub_ctx(agent_id: str = "agent_test") -> HostMcpContext:
    return HostMcpContext(
        agent_id=agent_id,
        slug="bot-test",
        operator_slug="op-test",
        host_home=Any,   # handlers are stubbed in these tests
        agent_home=Any,
        harness="claude-code",
        keystore=None,
        http_client=None,
    )


@pytest.fixture
def app_client_factory():
    """Async fixtures cause pytest_asyncio warnings here; hand the
    test a builder that returns an entered async client. Each test
    manages teardown via the cleanup function the factory returns."""
    created: list = []
    async def _make():
        cfg = rpc_service.RpcServiceConfig(enabled=True, port=0)
        app = rpc_service.build_app(cfg)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        created.append(client)
        return client
    yield _make
    # Sync teardown — clients close themselves when their event loop
    # tears down, but clearing the global resolver is non-async.
    rpc_service.set_rpc_resolver(None)


@pytest.mark.asyncio
async def test_install_503_when_resolver_unset(app_client_factory):
    rpc_service.set_rpc_resolver(None)
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_test/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "node"}},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "rpc resolver not wired" in body["error"]


@pytest.mark.asyncio
async def test_install_404_when_resolver_returns_none(app_client_factory):
    rpc_service.set_rpc_resolver(lambda _aid: None)
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/missing/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "node"}},
    )
    assert resp.status == 404
    body = await resp.json()
    assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_install_dispatches_to_handler(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    """Happy path: resolver returns a ctx, handler returns a
    message, route packages it as {"message": ...}."""
    captured: dict[str, Any] = {}
    async def _stub_install(ctx, *, name, template_id, spec):
        captured.update(
            ctx=ctx, name=name, template_id=template_id, spec=spec,
        )
        return "installed!"
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": "x", "template_id": "gmail-read"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"message": "installed!"}
    assert captured["name"] == "x"
    assert captured["template_id"] == "gmail-read"
    # template_id was provided but spec was omitted in the body — the
    # route normalises missing fields to None for spec.
    assert captured["spec"] is None
    assert captured["ctx"].agent_id == "agent_a"


@pytest.mark.asyncio
async def test_install_runtimerror_surfaces_as_400(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    async def _stub_install(ctx, *, name, template_id, spec):
        raise RuntimeError("install_host_mcp: name is required")
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": ""},
    )

    assert resp.status == 400
    body = await resp.json()
    assert "name is required" in body["error"]


@pytest.mark.asyncio
async def test_install_unexpected_exception_500(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    async def _stub_install(ctx, *, name, template_id, spec):
        raise ValueError("boom")
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "install", _stub_install,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        json={"name": "x", "spec": {"type": "stdio", "command": "n"}},
    )

    assert resp.status == 500
    body = await resp.json()
    assert "boom" in body["error"]


@pytest.mark.asyncio
async def test_install_rejects_non_json_body(app_client_factory):
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_a/install-mcp",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_sync_dispatches_to_handler(app_client_factory, monkeypatch):
    app_client = await app_client_factory()
    captured: dict[str, Any] = {}
    async def _stub_sync(ctx, *, template_id):
        captured.update(ctx=ctx, template_id=template_id)
        return "synced!"
    monkeypatch.setattr(
        rpc_service.host_mcp_handler, "sync", _stub_sync,
    )
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid))

    resp = await app_client.post(
        "/v1/rpc/agent_a/sync-mcp",
        json={"template_id": "gmail-read"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"message": "synced!"}
    assert captured["template_id"] == "gmail-read"
