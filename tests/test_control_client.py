"""execute_command applies portal commands to local agent state."""

from __future__ import annotations

import json
import types

import pytest

from _bridge_support import isolated_home, write_test_agent
from puffo_agent.portal.control import client as cc
from puffo_agent.portal.control.client import MachineControlClient, execute_command
from puffo_agent.portal.state import AgentConfig


class _FakeWS:
    def __init__(self):
        self.acks = []

    async def send_json(self, obj):
        self.acks.append(obj)


@pytest.mark.asyncio
async def test_handle_rejects_replayed_nonce_and_bounds_set(monkeypatch):
    executed = []

    async def _fake_exec(op, slug, params, **kw):
        executed.append(slug)
        return {"ok": True}

    monkeypatch.setattr(cc, "execute_command", _fake_exec)
    monkeypatch.setattr(cc, "load_pairings", lambda: {
        "op": types.SimpleNamespace(operator_root_pubkey="ROOT", server_url="https://s"),
    })
    monkeypatch.setattr(
        cc, "decrypt_command",
        lambda env, machine, root, now: {"op": "pause", "agent_slug": "a1", "params": {}},
    )
    monkeypatch.setattr(cc, "now_ms", lambda: 1_000_000)

    mc = MachineControlClient(machine=object())
    ws = _FakeWS()

    def frame(cid, nonce, ts=1_000_000):
        return {"command_id": cid, "operator_slug": "op",
                "envelope": {"nonce": nonce, "ts": ts}}

    await mc._handle(ws, frame("c1", "N1"))
    assert executed == ["a1"]
    assert "N1" in mc._seen_nonces
    assert ws.acks[-1] == {"type": "ack", "command_id": "c1"}

    # replayed nonce → not executed again, but still acked so it stops redelivering
    await mc._handle(ws, frame("c2", "N1"))
    assert executed == ["a1"]
    assert ws.acks[-1] == {"type": "ack", "command_id": "c2"}

    # a nonce older than the ts window is pruned on the next handled command
    mc._seen_nonces["OLD"] = 1_000_000 - cc.TS_WINDOW_MS - 1
    await mc._handle(ws, frame("c3", "N2"))
    assert "OLD" not in mc._seen_nonces
    assert "N2" in mc._seen_nonces


@pytest.fixture
def home(monkeypatch):
    h = isolated_home()
    yield h


@pytest.mark.asyncio
async def test_pause_then_resume(home):
    write_test_agent(home, "scout")
    assert AgentConfig.load("scout").state == "running"

    res = await execute_command("pause", "scout", {})
    assert res["ok"] is True
    assert AgentConfig.load("scout").state == "paused"

    res = await execute_command("resume", "scout", {})
    assert res["ok"] is True
    assert AgentConfig.load("scout").state == "running"


@pytest.mark.asyncio
async def test_edit_display_name_and_role(home):
    write_test_agent(home, "scout")
    res = await execute_command("edit", "scout", {"display_name": "Scout One", "role": "researcher"})
    assert res["ok"] is True
    cfg = AgentConfig.load("scout")
    assert cfg.display_name == "Scout One"
    assert cfg.role == "researcher"


@pytest.mark.asyncio
async def test_archive_drops_flag(home):
    from puffo_agent.portal.state import archive_flag_path

    write_test_agent(home, "scout")
    res = await execute_command("archive", "scout", {})
    assert res["ok"] is True
    assert archive_flag_path("scout").exists()


@pytest.mark.asyncio
async def test_unknown_agent_rejected(home):
    res = await execute_command("pause", "ghost", {})
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_unsupported_op_rejected(home):
    res = await execute_command("export", "scout", {})
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_without_pending_token_rejected(home):
    # create needs the operator pairing context + a pending_token; without them
    # it must reject before touching the server or disk.
    res = await execute_command(
        "create", None, {"identity_bundle": {}},
        server_url="http://localhost:3000", paired_root_pubkey="cGs=",
    )
    assert res["ok"] is False
    assert "pending_token" in res["error"]


# ── usage-report snapshot loop ─────────────────────────────────────


class _FakeResp:
    def __init__(self, status):
        self.status = status


class _FakeSession:
    def __init__(self, status, calls):
        self._status, self._calls = status, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, headers=None):
        self._calls.append({"url": url, "data": data, "headers": headers})
        return _FakeResp(self._status)


def _wire_usage(monkeypatch, *, snapshot, status, calls):
    async def _collect(_home):
        return snapshot

    monkeypatch.setattr(cc, "collect_usage_snapshot", _collect)
    monkeypatch.setattr(cc, "load_pairings", lambda: {
        "op": types.SimpleNamespace(server_url="https://s/", operator_root_pubkey="R"),
    })
    monkeypatch.setattr(cc.machine_auth, "signed_headers", lambda *a, **k: {"x-sig": "1"})
    monkeypatch.setattr(
        cc, "create_remote_http_session", lambda base, **k: _FakeSession(status, calls)
    )

    async def _stop_after(stop, timeout):
        stop.set()  # run exactly one loop iteration

    monkeypatch.setattr(cc, "_sleep_or_stop", _stop_after)


@pytest.mark.asyncio
async def test_usage_loop_posts_the_machine_snapshot(monkeypatch):
    calls = []
    snap = {"claude-code": {"session": {"used_pct": 41, "resets_at": "x"}}}
    _wire_usage(monkeypatch, snapshot=snap, status=200, calls=calls)
    await cc.ControlManager()._usage_loop(types.SimpleNamespace(machine_id="mac_1"))
    assert len(calls) == 1
    assert calls[0]["url"] == "https://s/v2/machines/mac_1/usage"
    assert json.loads(calls[0]["data"]) == {"snapshot": snap}


@pytest.mark.asyncio
async def test_usage_loop_skips_post_when_no_snapshot(monkeypatch):
    calls = []
    _wire_usage(monkeypatch, snapshot=None, status=200, calls=calls)
    await cc.ControlManager()._usage_loop(types.SimpleNamespace(machine_id="mac_1"))
    assert calls == []


@pytest.mark.asyncio
async def test_usage_loop_tolerates_http_error(monkeypatch):
    calls = []
    snap = {"claude-code": {"session": {"used_pct": 1, "resets_at": "x"}}}
    _wire_usage(monkeypatch, snapshot=snap, status=500, calls=calls)
    # Best-effort: a 5xx must not raise; the loop finishes its iteration.
    await cc.ControlManager()._usage_loop(types.SimpleNamespace(machine_id="mac_1"))
    assert len(calls) == 1


# ── refresh_usage command (on-demand snapshot POST) ────────────────


def _wire_refresh_usage(monkeypatch, *, snapshot, status, calls):
    async def _collect(_home):
        return snapshot

    monkeypatch.setattr(cc, "collect_usage_snapshot", _collect)
    monkeypatch.setattr(cc, "load_or_create_machine", lambda: types.SimpleNamespace(machine_id="mac_1"))
    monkeypatch.setattr(cc.machine_auth, "signed_headers", lambda *a, **k: {"x-sig": "1"})
    monkeypatch.setattr(
        cc, "create_remote_http_session", lambda base, **k: _FakeSession(status, calls)
    )


@pytest.mark.asyncio
async def test_refresh_usage_posts_now(monkeypatch):
    calls = []
    snap = {"claude-code": {"session": {"used_pct": 2}}}
    _wire_refresh_usage(monkeypatch, snapshot=snap, status=200, calls=calls)
    res = await cc.execute_command("refresh_usage", None, {}, server_url="https://s/")
    assert res == {"ok": True, "posted": True}
    assert len(calls) == 1
    assert calls[0]["url"] == "https://s/v2/machines/mac_1/usage"
    assert json.loads(calls[0]["data"]) == {"snapshot": snap}


@pytest.mark.asyncio
async def test_refresh_usage_no_snapshot_posts_nothing(monkeypatch):
    calls = []
    _wire_refresh_usage(monkeypatch, snapshot=None, status=200, calls=calls)
    res = await cc.execute_command("refresh_usage", None, {}, server_url="https://s/")
    assert res == {"ok": True, "posted": False}
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_usage_without_server_url_errors():
    res = await cc.execute_command("refresh_usage", None, {})
    assert res["ok"] is False
    assert "server_url" in res["error"]
