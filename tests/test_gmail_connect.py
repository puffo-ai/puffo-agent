"""Gmail connect: presence gate, loopback refusal, sanitized projection.

The negative controls are the contract: no confirmation → the executor
is never spawned; a non-loopback callback is refused before spawn; and
nothing token-shaped can reach the status file or its consumers.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap

import pytest

from puffo_agent.portal.gmail_connect import executor as executor_mod
from puffo_agent.portal.gmail_connect import ops
from puffo_agent.portal.gmail_connect.executor import (
    ExecutorRefused,
    run_gmail_executor,
)
from puffo_agent.portal.gmail_connect.status_store import (
    GmailConnectStatus,
    load_status,
    mask_account,
    status_path,
    store_status,
)
from puffo_agent.portal.state import DaemonConfig, GmailConnectConfig


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _configured(monkeypatch, **overrides) -> None:
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True, executor_path="/usr/bin/true", **overrides
    )
    monkeypatch.setattr(ops, "_config", lambda: cfg)


@pytest.mark.asyncio
async def test_declined_confirm_never_spawns_executor(home, monkeypatch):
    """The native confirm is the gate: decline → zero executor calls and
    no state transition. Removing the gate in ``gmail_connect_initiate``
    turns this red."""
    _configured(monkeypatch)
    calls: list[dict] = []

    async def deny(prompt, *, timeout_s):
        return False

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        calls.append({})
        return executor_mod.ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", deny)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "error": "user_declined"}
    assert calls == []
    assert load_status().state == "disconnected"


@pytest.mark.asyncio
async def test_non_loopback_callback_refused_before_spawn(monkeypatch):
    spawned: list[object] = []

    async def spy_spawn(*a, **k):  # pragma: no cover - must not run
        spawned.append(a)
        raise AssertionError("spawn must not happen")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_spawn)
    with pytest.raises(ExecutorRefused):
        await run_gmail_executor(
            "/usr/bin/true",
            {"op": "connect", "callback_host": "0.0.0.0"},
            flow_timeout_s=5,
        )
    assert spawned == []


@pytest.mark.asyncio
async def test_confirmed_flow_persists_masked_sanitized_projection(home, monkeypatch):
    _configured(monkeypatch)

    async def allow(prompt, *, timeout_s):
        return True

    async def fake_executor(entrypoint, request, *, flow_timeout_s):
        return executor_mod.ExecutorOutcome(
            status="connected",
            account="jeremy.shen@gmail.com",
            expires_at="2026-10-06T00:00:00Z",
        )

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", fake_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": True, "state": "connected"}
    raw = status_path().read_text(encoding="utf-8")
    assert "jeremy.shen" not in raw
    on_disk = json.loads(raw)
    # Whitelist exactly: nothing token-shaped can even be represented.
    assert set(on_disk) == {
        "state", "account_masked", "expires_at", "reason", "updated_at",
    }
    assert on_disk["account_masked"] == "je***@gmail.com"
    mode = stat.S_IMODE(os.stat(status_path()).st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_disconnect_clears_locally_even_when_revoke_unconfirmed(
    home, monkeypatch
):
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="connected", account_masked="je***@x"))

    async def broken_executor(entrypoint, request, *, flow_timeout_s):
        assert request["op"] == "revoke"
        return executor_mod.ExecutorOutcome(status="failed", reason="timeout")

    monkeypatch.setattr(ops, "run_gmail_executor", broken_executor)

    res = await ops.gmail_disconnect_token({})

    assert res["ok"] is True
    assert res["scope"] == "token_only"
    after = load_status()
    assert after.state == "disconnected"
    assert after.account_masked == ""
    assert after.reason == "revoke_unconfirmed"


@pytest.mark.asyncio
async def test_token_only_disconnect_never_yields_revoked_projection(
    home, monkeypatch
):
    """Runbook v6 §7 negative control: ``revoked`` belongs to the
    composite Disconnect (grant + token both revoked). A token-only
    success — even an executor *claiming* revoked — must cap the
    projection at ``disconnected``."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="connected", account_masked="je***@x"))

    async def overclaiming_executor(entrypoint, request, *, flow_timeout_s):
        return executor_mod.ExecutorOutcome(status="revoked")

    monkeypatch.setattr(ops, "run_gmail_executor", overclaiming_executor)

    res = await ops.gmail_disconnect_token({})

    assert res["scope"] == "token_only"
    assert load_status().state == "disconnected"
    assert load_status().state != "revoked"


@pytest.mark.asyncio
async def test_control_dispatch_routes_machine_level_gmail_ops(monkeypatch):
    """``execute_command`` must reach the gmail ops with no agent_slug —
    the agent-existence guard would otherwise swallow machine ops."""
    from puffo_agent.portal.control.client import execute_command

    async def fake_initiate(params):
        return {"ok": True, "state": "marker"}

    monkeypatch.setattr(ops, "gmail_connect_initiate", fake_initiate)
    res = await execute_command("gmail.connect_initiate", None, {})
    assert res == {"ok": True, "state": "marker"}

    async def fake_disconnect(params):
        return {"ok": True, "scope": "token_only"}

    monkeypatch.setattr(ops, "gmail_disconnect_token", fake_disconnect)
    res = await execute_command("gmail.disconnect_token", None, {})
    assert res == {"ok": True, "scope": "token_only"}


def _fake_executor_script(tmp_path, body: str) -> str:
    path = tmp_path / "fake_executor.py"
    path.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
    )
    path.chmod(0o755)
    return str(path)


@pytest.mark.asyncio
async def test_executor_protocol_stdin_json_two_lines(tmp_path):
    """Real subprocess: request travels via stdin (never argv/env), and
    the two-line ready/result protocol parses into the outcome."""
    script = _fake_executor_script(
        tmp_path,
        f'''
        import json, sys
        req = json.loads(sys.stdin.readline())
        assert req["op"] == "connect"
        assert len(sys.argv) == 1
        print(json.dumps({{"ready": True}})); sys.stdout.flush()
        print(json.dumps({{
            "status": "connected",
            "account": "a@b.c",
            "expires_at": "e",
        }})); sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"op": "connect", "callback_host": "127.0.0.1"},
        flow_timeout_s=10,
    )
    assert outcome.status == "connected"
    assert outcome.account == "a@b.c"


@pytest.mark.asyncio
async def test_executor_silent_after_ready_is_killed_as_timeout(tmp_path):
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"ready": True})); sys.stdout.flush()
        time.sleep(600)
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"op": "connect", "callback_host": "127.0.0.1"},
        flow_timeout_s=0.5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "timeout"


def test_mask_account_never_keeps_full_local_part(home):
    assert mask_account("jeremy.shen@gmail.com") == "je***@gmail.com"
    assert mask_account("a@b.c") == "a***@b.c"
    assert mask_account("not-an-email") == "***"
    assert mask_account("") == ""


@pytest.mark.asyncio
async def test_status_route_serves_projection_only(home):
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import rpc_service
    from puffo_agent.portal.local_service_auth import (
        issue_local_service_token,
        local_service_headers,
    )

    store_status(GmailConnectStatus(state="connected", account_masked="je***@x"))
    cfg = rpc_service.RpcServiceConfig(enabled=True, port=0)
    app = rpc_service.build_app(cfg)
    headers = local_service_headers(issue_local_service_token("any-agent"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/rpc/any-agent/gmail-connect-status",
            json={},
            headers=headers,
        )
        assert resp.status == 200
        body = await resp.json()
    assert body == {
        "ok": True,
        "state": "connected",
        "account_masked": "je***@x",
        "expires_at": "",
        "reason": "",
    }
