"""Gmail connect: presence gate, loopback checks, Layer-B projection.

The negative controls are the contract: no confirmation → the executor
is never spawned; a non-loopback callback or ready line dies before/at
the seam; token-only disconnect never reads as ``revoked``; and no
Layer-A detail (token_db, bundle paths, scope) can reach the Layer-B
projection or its consumers (EXECUTOR_INVOKE_SCHEMA v1 §5.1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import textwrap

import pytest

from puffo_agent.portal.gmail_connect import executor as executor_mod
from puffo_agent.portal.gmail_connect import ops
from puffo_agent.portal.gmail_connect.executor import (
    ExecutorOutcome,
    ExecutorRefused,
    run_gmail_executor,
)
from puffo_agent.portal.gmail_connect.status_store import (
    STATES,
    GmailConnectStatus,
    load_status,
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
        enabled=True,
        executor_path="/usr/bin/true",
        data_root="/var/lib/gw",
        client_bundle_sha256="a" * 64,
        **overrides,
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
        return ExecutorOutcome(status="connected")

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
            {"data_root": "/d", "expected_sha256": "a" * 64,
             "callback_host": "0.0.0.0"},
            flow_timeout_s=5,
        )
    assert spawned == []


@pytest.mark.asyncio
async def test_confirmed_flow_persists_layer_b_projection_only(home, monkeypatch):
    _configured(monkeypatch)
    sent: list[dict] = []

    async def allow(prompt, *, timeout_s):
        return True

    async def fake_executor(entrypoint, request, *, flow_timeout_s):
        sent.append(request)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", fake_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": True, "state": "connected"}
    # Invoke config comes from local daemon config, never from params.
    assert sent == [{
        "data_root": "/var/lib/gw", "expected_sha256": "a" * 64, "timeout": 300.0,
    }]
    on_disk = json.loads(status_path().read_text(encoding="utf-8"))
    # Layer B whitelist exactly (design v1.6 §3): state + reason.
    assert set(on_disk) == {"state", "reason", "updated_at"}
    assert on_disk["state"] == "connected"
    mode = stat.S_IMODE(os.stat(status_path()).st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_failed_reason_reaches_projection_coarse(home, monkeypatch):
    _configured(monkeypatch)

    async def allow(prompt, *, timeout_s):
        return True

    async def failing_executor(entrypoint, request, *, flow_timeout_s):
        return ExecutorOutcome(status="failed", reason="callback_timeout")

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", failing_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "error": "callback_timeout", "state": "failed"}
    after = load_status()
    assert (after.state, after.reason) == ("failed", "callback_timeout")


@pytest.mark.asyncio
async def test_token_only_disconnect_never_yields_revoked(home, monkeypatch):
    """Runbook v6 §7 negative control: ``revoked`` belongs to the
    composite Disconnect (grant + token both revoked). The token-only
    op caps the projection at ``disconnected`` and records the remote
    revoke as unconfirmed (invoke schema v1 has no revoke axis yet)."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="connected"))

    res = await ops.gmail_disconnect_token({})

    assert res["ok"] is True
    assert res["scope"] == "token_only"
    after = load_status()
    assert after.state == "disconnected"
    assert after.state != "revoked"
    assert after.reason == "revoke_unconfirmed"


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
async def test_executor_schema_v1_protocol_and_layer_a_drop(tmp_path, home):
    """Real subprocess against invoke schema v1: config travels via one
    stdin JSON line (never argv), the ready/result event lines parse,
    and the Layer-A ``summary`` (token_db, scope, …) is dropped by
    construction — the outcome type carries only status + reason and
    the persisted projection scans clean."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        req = json.loads(sys.stdin.readline())
        assert req["data_root"] == "/d"
        assert req["expected_sha256"] == "a" * 64
        assert len(sys.argv) == 1
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        print(json.dumps({"event": "result", "status": "connected",
            "summary": {"scope": "gmail.send", "expires_in": 3599,
                        "has_refresh_token": True,
                        "token_db": "/private/secrets/tokens.db"}}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 5.0},
        flow_timeout_s=10,
    )
    assert dataclasses.asdict(outcome) == {"status": "connected", "reason": ""}
    store_status(GmailConnectStatus(state="connected"))
    raw = status_path().read_text(encoding="utf-8")
    for leaked in ("token_db", "scope", "expires_in", "/private/secrets"):
        assert leaked not in raw


@pytest.mark.asyncio
async def test_prebind_failure_reason_survives_missing_ready(tmp_path):
    """Schema v1.1 §3: a pre-bind failure is 0 ready + 1 failed result.
    The daemon must surface the executor's real reason — mapping a
    missing ready line to timeout/protocol wholesale turns this red."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "failed",
                          "reason": "bundle_verify_failed"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "bundle_verify_failed"


@pytest.mark.asyncio
async def test_out_of_enum_reason_is_clamped_to_internal_error(tmp_path):
    """The reason field is a closed enum (schema §4, six values); a
    free-text reason could carry secrets into the Layer-B projection,
    so the daemon clamps anything unknown to internal_error."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "failed",
                          "reason": "Exception: token=SENTINEL_ya29_SECRET"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.reason == "internal_error"
    assert "SENTINEL" not in outcome.reason


@pytest.mark.asyncio
async def test_connected_without_ready_is_protocol_failure(tmp_path):
    """A connected result with no prior ready line is out of contract:
    a flow that never bound loopback cannot have run consent."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "connected"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "protocol"


@pytest.mark.asyncio
async def test_non_loopback_ready_line_kills_the_flow(tmp_path):
    """An executor advertising a routable redirect_uri is not the
    contract's executor: the daemon must kill it at the ready line,
    before any browser or consent could be in flight."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://evil.example:80/oauth2/callback"}))
        sys.stdout.flush()
        time.sleep(600)
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "non_loopback_ready"


@pytest.mark.asyncio
async def test_executor_silent_after_ready_is_killed_as_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(executor_mod, "RESULT_DEADLINE_MARGIN_S", 0.2)
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        time.sleep(600)
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=0.3,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "timeout"


@pytest.mark.asyncio
async def test_status_route_serves_projection_only(home):
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import rpc_service
    from puffo_agent.portal.local_service_auth import (
        issue_local_service_token,
        local_service_headers,
    )

    store_status(GmailConnectStatus(state="connected"))
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
    # Layer B exactly: any extra key (a Layer-A path, an account, …)
    # turns this red — the assertion is a whitelist, not a keyword scan.
    assert body == {"ok": True, "state": "connected", "reason": ""}


LEAK = "token exchange failed db=/private/secrets/tokens.db client_secret=GOCSPX-abc"


@pytest.mark.asyncio
async def test_out_of_roster_reason_is_clamped_before_layer_b(tmp_path, home, monkeypatch):
    """End-to-end via a real subprocess: a hostile/buggy executor must not
    put free text into the on-disk file, the Layer-B projection, or the
    control-plane response body.

    Negative control authored by Boris (review of e926d42, patch
    85fece2b…); kept as-is because it covers an exit the unit-level
    clamp test does not — ``ops`` returns the reason as ``error``
    without passing through ``projection()``.
    """
    script = _fake_executor_script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        print(json.dumps({"event": "result", "status": "failed",
            "reason": %r}))
        sys.stdout.flush()
        """ % LEAK,
    )
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True, executor_path=script, data_root="/d",
        client_bundle_sha256="a" * 64,
    )
    monkeypatch.setattr(ops, "_config", lambda: cfg)
    async def allow(prompt, timeout_s=0.0):
        return True

    monkeypatch.setattr(ops, "request_native_confirm", allow)

    res = await ops.gmail_connect_initiate({})
    on_disk = status_path().read_text(encoding="utf-8")
    projection = load_status().projection()

    assert "client_secret" not in on_disk and "/private/secrets" not in on_disk
    assert projection["reason"] == "internal_error"
    assert "client_secret" not in json.dumps(res)


def test_poisoned_status_file_cannot_leak_through_the_load_path(home):
    """The trust-boundary clamp does not cover bytes already on disk.

    A file written by an older build (or a rollback, or a hand edit)
    is untrusted input too: ``load_status`` must clamp it, or the
    projection faithfully serves whatever is in the file.
    """
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "failed", "reason": LEAK}), encoding="utf-8")

    projection = load_status().projection()

    assert projection["reason"] == "internal_error"
    assert "client_secret" not in json.dumps(projection)


def test_revoked_is_structurally_absent_not_merely_unused(home):
    """``revoked`` belongs to the composite Disconnect, which does not
    exist yet. "No call site mints it" is an enumeration of today's code;
    this pins the structural form — it is not constructible and not
    loadable, so an on-disk ``revoked`` cannot be served to the UI as a
    revocation that never happened.
    """
    assert "revoked" not in STATES

    assert GmailConnectStatus(state="revoked").state == "disconnected"

    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "revoked", "reason": ""}), encoding="utf-8")
    assert load_status().state == "disconnected"
