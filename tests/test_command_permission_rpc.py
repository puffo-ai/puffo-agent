"""cli-local command permission over the daemon RPC: the
/v1/rpc/{agent}/permission-request route, the host_mcp_handler
validation layer, and the hook's puffo-core transport."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import rpc_service
from puffo_agent.portal import host_mcp_handler
from puffo_agent.portal.host_mcp_handler import HostMcpContext


class _StubMessageClient:
    def __init__(self, decision: str = "allow"):
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def request_command_permission(self, *, tool_name, summary, timeout_s):
        self.calls.append(
            {"tool_name": tool_name, "summary": summary, "timeout_s": timeout_s}
        )
        return self.decision


def _stub_ctx(agent_id: str = "agent_test", message_client=None) -> HostMcpContext:
    return HostMcpContext(
        agent_id=agent_id,
        slug="bot-test",
        operator_slug="op-test",
        host_home=Any,
        agent_home=Any,
        harness="claude-code",
        keystore=None,
        http_client=None,
        message_client=message_client,
    )


@pytest.fixture
def app_client_factory():
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
    rpc_service.set_rpc_resolver(None)


@pytest.mark.asyncio
async def test_permission_request_roundtrips_decision(app_client_factory):
    mc = _StubMessageClient("allow")
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid, message_client=mc))
    app_client = await app_client_factory()

    resp = await app_client.post(
        "/v1/rpc/agent_a/permission-request",
        json={"tool_name": "Bash", "summary": "- command: ls", "timeout_s": 42},
    )
    assert resp.status == 200
    assert (await resp.json()) == {"message": "allow"}
    assert mc.calls == [
        {"tool_name": "Bash", "summary": "- command: ls", "timeout_s": 42}
    ]


@pytest.mark.asyncio
async def test_permission_request_defaults_and_clamps_timeout(app_client_factory):
    mc = _StubMessageClient("deny")
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid, message_client=mc))
    app_client = await app_client_factory()

    # Missing timeout → 300; absurd timeout → clamped to 3600.
    await app_client.post(
        "/v1/rpc/agent_a/permission-request", json={"tool_name": "Bash"},
    )
    await app_client.post(
        "/v1/rpc/agent_a/permission-request",
        json={"tool_name": "Bash", "timeout_s": 999999},
    )
    assert [c["timeout_s"] for c in mc.calls] == [300, 3600]
    # Non-numeric timeout → default, not a 500.
    resp = await app_client.post(
        "/v1/rpc/agent_a/permission-request",
        json={"tool_name": "Bash", "timeout_s": "soon"},
    )
    assert resp.status == 200
    assert mc.calls[-1]["timeout_s"] == 300


@pytest.mark.asyncio
async def test_permission_request_requires_tool_name(app_client_factory):
    rpc_service.set_rpc_resolver(
        lambda aid: _stub_ctx(aid, message_client=_StubMessageClient()),
    )
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_a/permission-request", json={"summary": "x"},
    )
    assert resp.status == 400
    assert "tool_name" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_permission_request_400_when_worker_cold(app_client_factory):
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid, message_client=None))
    app_client = await app_client_factory()
    resp = await app_client.post(
        "/v1/rpc/agent_a/permission-request", json={"tool_name": "Bash"},
    )
    assert resp.status == 400
    assert "warm" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_permission_request_end_to_end_over_socket(app_client_factory):
    """Route → handler → real request_command_permission over a live
    socket, with the operator's `y` landing while the RPC is held open."""
    import asyncio
    import logging
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = "op-1"
    client._pending_command_permissions = {}
    client._log = logging.getLogger("rpc-e2e-test")
    sent: list[dict] = []

    async def _stub_send_dm(slug, text, root_id=""):
        env_id = f"env_{len(sent) + 1}"
        sent.append({"to": slug, "text": text, "root_id": root_id, "env_id": env_id})
        return {"envelope_id": env_id}

    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    rpc_service.set_rpc_resolver(lambda aid: _stub_ctx(aid, message_client=client))
    app_client = await app_client_factory()

    async def _reply_when_prompted():
        while not sent:
            await asyncio.sleep(0.01)
        await client._maybe_handle_permission_reply(
            thread_root_id=sent[0]["env_id"], text="y",
        )

    replier = asyncio.ensure_future(_reply_when_prompted())
    resp = await app_client.post(
        "/v1/rpc/agent-1/permission-request",
        json={"tool_name": "Bash", "summary": "- command: ls", "timeout_s": 10},
    )
    await replier
    assert resp.status == 200
    assert (await resp.json()) == {"message": "allow"}
    assert sent[0]["text"].startswith("/permission ")
    assert any("Approved" in d["text"] for d in sent)


# ─── hook transport (puffo-core branch) ───────────────────────────────


def _run_rpc_hook(monkeypatch, capsys, *, response=None, error=None):
    """Drive hooks.permission.request_via_rpc with a stubbed transport;
    returns (SystemExit.code, stdout, stderr)."""
    from puffo_agent.hooks import permission as hook

    def _stub_post(url, headers, payload, timeout=10.0):
        _stub_post.called = {"url": url, "payload": payload, "timeout": timeout}
        if error is not None:
            raise error
        return response

    _stub_post.called = None
    monkeypatch.setattr(hook, "_http_post", _stub_post)
    with pytest.raises(SystemExit) as exc:
        hook.request_via_rpc("http://127.0.0.1:63385", "agent-1", "Bash", "- x", 60)
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err, _stub_post.called


def test_hook_rpc_allow_emits_allow_json(monkeypatch, capsys):
    code, out, _err, called = _run_rpc_hook(
        monkeypatch, capsys, response={"message": "allow"},
    )
    assert code == 0
    decision = json.loads(out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert called["url"].endswith("/v1/rpc/agent-1/permission-request")
    assert called["payload"]["tool_name"] == "Bash"
    # Transport padded past the decision window.
    assert called["timeout"] > 60


def test_hook_rpc_deny_exits_2(monkeypatch, capsys):
    code, _out, err, _ = _run_rpc_hook(
        monkeypatch, capsys, response={"message": "deny"},
    )
    assert code == 2
    assert "denied" in err


def test_hook_rpc_timeout_exits_2(monkeypatch, capsys):
    code, _out, err, _ = _run_rpc_hook(
        monkeypatch, capsys, response={"message": "timeout"},
    )
    assert code == 2
    assert "timed out" in err


def test_hook_rpc_transport_error_fails_open(monkeypatch, capsys):
    code, out, err, _ = _run_rpc_hook(
        monkeypatch, capsys, error=OSError("connection refused"),
    )
    assert code == 0
    assert out == ""  # no allow JSON — claude falls through to native flow
    assert "fail-open" in err


def test_hook_rpc_unexpected_decision_fails_open(monkeypatch, capsys):
    code, out, _err, _ = _run_rpc_hook(
        monkeypatch, capsys, response={"message": "maybe"},
    )
    assert code == 0
    assert out == ""


def test_hook_main_routes_to_rpc_when_no_legacy_creds(monkeypatch, capsys):
    from puffo_agent.hooks import permission as hook

    monkeypatch.delenv("PUFFO_URL", raising=False)
    monkeypatch.delenv("PUFFO_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PUFFO_RPC_URL", "http://127.0.0.1:63385")
    monkeypatch.setenv("PUFFO_AGENT_ID", "agent-9")
    monkeypatch.setenv("PUFFO_PERMISSION_TIMEOUT", "45")

    captured: dict[str, Any] = {}

    def _stub_rpc(rpc_url, agent_id, tool_name, summary, timeout_s):
        captured.update(
            rpc_url=rpc_url, agent_id=agent_id, tool_name=tool_name,
            summary=summary, timeout_s=timeout_s,
        )
        raise SystemExit(0)

    monkeypatch.setattr(hook, "request_via_rpc", _stub_rpc)
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        )),
    )
    with pytest.raises(SystemExit):
        hook.main()
    assert captured["agent_id"] == "agent-9"
    assert captured["tool_name"] == "Bash"
    assert "ls" in captured["summary"]
    assert captured["timeout_s"] == 45


def test_hook_main_rpc_branch_fails_open_on_bad_stdin(monkeypatch, capsys):
    from puffo_agent.hooks import permission as hook

    monkeypatch.delenv("PUFFO_URL", raising=False)
    monkeypatch.delenv("PUFFO_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PUFFO_RPC_URL", "http://127.0.0.1:63385")
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    with pytest.raises(SystemExit) as exc:
        hook.main()
    assert exc.value.code == 0
    assert "could not parse hook payload" in capsys.readouterr().err


def test_hook_main_fails_open_without_any_transport(monkeypatch, capsys):
    from puffo_agent.hooks import permission as hook

    for var in ("PUFFO_URL", "PUFFO_BOT_TOKEN", "PUFFO_RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        hook.main()
    assert exc.value.code == 0
    assert "fail-open" in capsys.readouterr().err
