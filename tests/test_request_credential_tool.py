"""The ``request_credential`` / ``credential_status`` tools, handler and RPC."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from puffo_agent.mcp.config import PUFFO_CORE_TOOL_NAMES
from puffo_agent.mcp.core_host_mcp_tools import register_host_mcp_tools
from puffo_agent.portal import credential_requests as cr
from puffo_agent.portal import host_mcp_handler
from puffo_agent.portal.host_mcp_handler import HostMcpContext

OP = "op-test"


class _Coordinator:
    def __init__(self, state="sent"):
        self.sent = []
        self.state = state

    async def send(self, request):
        self.sent.append(request)
        return {"state": self.state, "error": "nope" if self.state == "failed" else None}


def _ctx(tmp_path, coordinator, harness="claude-code"):
    return HostMcpContext(
        agent_id="agent_test", slug="bot-test", operator_slug=OP,
        host_home=tmp_path / "host", agent_home=tmp_path / "agent", harness=harness,
        keystore=None, http_client=None, send_coordinator=coordinator,
    )


@pytest.fixture
def agent_dir(tmp_path, monkeypatch):
    from puffo_agent.portal import state

    d = tmp_path / "agents"
    monkeypatch.setattr(state, "agent_dir", lambda agent_id: d / agent_id)
    monkeypatch.setattr("puffo_agent.macos.keychain.is_macos", lambda: False)
    return d / "agent_test"


@pytest.mark.asyncio
async def test_request_dms_the_owner_the_pinned_message_and_records_it(tmp_path, agent_dir):
    coord = _Coordinator()
    result = await host_mcp_handler.request_credential(
        _ctx(tmp_path, coord), type=cr.TYPE_CHATGPT, reason="to run Codex on my plan"
    )
    assert len(coord.sent) == 1
    req = coord.sent[0]
    assert req.destination == f"@{OP}"
    block = req.text.split(f"```{cr.REQUEST_FENCE}\n")[1].split("\n```")[0]
    data = json.loads(block)
    assert data["type"] == cr.TYPE_CHATGPT and data["reason"] == "to run Codex on my plan"
    ledger = cr.RequestLedger(agent_dir / "credential_requests.json")
    assert ledger.get(data["request_id"]).state == "pending"
    assert data["request_id"] in result and "credential_status" in result


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(type="PUFFO_NOPE_v1", reason="r"), "type must be one of"),
        (dict(type=cr.TYPE_CHATGPT, reason=""), "reason"),
        (dict(type=cr.TYPE_CHATGPT, reason="x" * 201), "reason"),
        (dict(type=cr.TYPE_CUSTOMIZED, reason="r"), "alias"),
        (dict(type=cr.TYPE_CUSTOMIZED, reason="r", alias="bad-name"), "alias"),
        (dict(type=cr.TYPE_CHATGPT, reason="r", alias="X"), "only for CUSTOMIZED"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_requests_are_refused_before_any_dm(tmp_path, agent_dir, kwargs, needle):
    coord = _Coordinator()
    with pytest.raises(RuntimeError, match=needle):
        await host_mcp_handler.request_credential(_ctx(tmp_path, coord), **kwargs)
    assert coord.sent == []


@pytest.mark.asyncio
async def test_a_claude_token_is_refused_on_macos_at_the_ask(tmp_path, agent_dir, monkeypatch):
    """subscription_credentials would refuse it at spawn (#37512); refusing at
    the ask spares the owner typing a token in for nothing."""
    monkeypatch.setattr("puffo_agent.macos.keychain.is_macos", lambda: True)
    coord = _Coordinator()
    with pytest.raises(RuntimeError, match="37512"):
        await host_mcp_handler.request_credential(_ctx(tmp_path, coord), type=cr.TYPE_CLAUDE_TOKEN, reason="r")
    assert coord.sent == []
    # the other two types are unaffected
    await host_mcp_handler.request_credential(_ctx(tmp_path, coord), type=cr.TYPE_CHATGPT, reason="r")
    await host_mcp_handler.request_credential(_ctx(tmp_path, coord), type=cr.TYPE_CUSTOMIZED, reason="r", alias="GH")
    assert len(coord.sent) == 2


@pytest.mark.asyncio
async def test_a_failed_dm_fails_the_request(tmp_path, agent_dir):
    with pytest.raises(RuntimeError, match="could not DM"):
        await host_mcp_handler.request_credential(_ctx(tmp_path, _Coordinator("failed")), type=cr.TYPE_CHATGPT, reason="r")
    ledger = cr.RequestLedger(agent_dir / "credential_requests.json")
    assert [r.state for r in ledger._items.values()] == ["failed"]


@pytest.mark.asyncio
async def test_status_reports_the_ledger_and_never_a_value(tmp_path, agent_dir):
    ctx = _ctx(tmp_path, _Coordinator())
    result = await host_mcp_handler.request_credential(ctx, type=cr.TYPE_CHATGPT, reason="r")
    rid = result.split("request_id ")[1].split(")")[0]
    assert (await host_mcp_handler.credential_status(ctx, request_id=rid)).startswith("pending")
    cr.RequestLedger(agent_dir / "credential_requests.json").settle(rid, state="placed", index=0, version=4)
    assert await host_mcp_handler.credential_status(ctx, request_id=rid) == (
        f"placed {cr.TYPE_CHATGPT} #0 v4; restarting my CLI to pick it up"
    )
    assert await host_mcp_handler.credential_status(ctx, request_id="nope") == "unknown request_id"
    with pytest.raises(RuntimeError):
        await host_mcp_handler.credential_status(ctx, request_id="")


# ── the MCP tool layer forwards keyword-only, and is listed ──────────────────


class _StubRpc:
    def __init__(self):
        self.calls = []

    async def request_credential(self, *, type, reason, alias=""):
        self.calls.append(("request", type, reason, alias))
        return "requested"

    async def credential_status(self, *, request_id):
        self.calls.append(("status", request_id))
        return "pending"


def _tools(rpc):
    mcp = FastMCP("test")
    register_host_mcp_tools(mcp, SimpleNamespace(rpc_client=rpc, workspace=None))
    return mcp._tool_manager._tools


@pytest.mark.asyncio
async def test_tools_forward_to_the_rpc_client():
    rpc = _StubRpc()
    tools = _tools(rpc)
    assert await tools["request_credential"].fn(type=cr.TYPE_CUSTOMIZED, reason="r", alias="GH") == "requested"
    assert await tools["credential_status"].fn(request_id="abc") == "pending"
    assert rpc.calls == [("request", cr.TYPE_CUSTOMIZED, "r", "GH"), ("status", "abc")]


@pytest.mark.asyncio
async def test_tools_without_rpc_give_guidance():
    tools = _tools(None)
    with pytest.raises(RuntimeError, match="PUFFO_RPC_URL"):
        await tools["request_credential"].fn(type=cr.TYPE_CHATGPT, reason="r")
    with pytest.raises(RuntimeError, match="PUFFO_RPC_URL"):
        await tools["credential_status"].fn(request_id="x")


def test_tools_are_in_the_core_tool_list():
    assert "request_credential" in PUFFO_CORE_TOOL_NAMES and "credential_status" in PUFFO_CORE_TOOL_NAMES


def test_tool_docstring_never_invites_a_secret_into_chat():
    doc = _tools(_StubRpc())["request_credential"].description or ""
    assert "never see it" in doc.lower() or "never see" in doc.lower()
    assert "credential_status" in doc


@pytest.mark.asyncio
async def test_rpc_client_posts_the_two_routes(monkeypatch):
    from puffo_agent.mcp._host_mcp import PuffoRpcClient

    posted = []

    async def _post(self, route, body):
        posted.append((route, body)); return "ok"

    monkeypatch.setattr(PuffoRpcClient, "_post", _post)
    c = PuffoRpcClient("http://127.0.0.1:1", "a1")
    await c.request_credential(type=cr.TYPE_CHATGPT, reason="r")
    await c.credential_status(request_id="rid")
    assert posted == [
        ("request-credential", {"type": cr.TYPE_CHATGPT, "reason": "r", "alias": ""}),
        ("credential-status", {"request_id": "rid"}),
    ]
