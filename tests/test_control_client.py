"""execute_command applies portal commands to local agent state."""

from __future__ import annotations

import asyncio
import json
import logging
import types

import pytest

from _portal_support import isolated_home, write_test_agent
from puffo_agent.portal.control import client as cc
from puffo_agent.portal.control.client import MachineControlClient, execute_command
from puffo_agent.portal.state import AgentConfig


class _FakeWS:
    def __init__(self):
        self.acks = []

    async def send_json(self, obj):
        self.acks.append(obj)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_frame", "connected_logged"),
    [
        ({"type": "error", "reason": "auth"}, False),
        ({"type": "connected"}, True),
    ],
)
async def test_control_logs_connected_only_after_server_acceptance(
    monkeypatch, caplog, server_frame, connected_logged,
):
    """A successful HTTP upgrade is not yet an authenticated control session."""
    class FakeSocket:
        def __init__(self):
            self.frames = [types.SimpleNamespace(
                type=cc.aiohttp.WSMsgType.TEXT,
                data=json.dumps(server_frame),
            )]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send_json(self, obj):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def ws_connect(self, *args, **kwargs):
            return FakeSocket()

    def fake_spawn(coro, *, name):
        coro.close()
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(
        cc, "load_pairings", lambda: {"op": types.SimpleNamespace(server_url="https://s")},
    )
    monkeypatch.setattr(cc, "create_remote_http_session", lambda base: FakeSession())
    monkeypatch.setattr(cc.machine_auth, "ws_connect_frame", lambda machine: {})
    monkeypatch.setattr(cc, "build_capabilities", lambda: {})
    monkeypatch.setattr(cc, "spawn", fake_spawn)
    caplog.set_level(logging.INFO, logger=cc.log.name)

    await MachineControlClient(machine=object())._connect_once(asyncio.Event())

    assert ("control: WS connected" in caplog.text) is connected_logged


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
    assert ws.acks[-1] == {
        "type": "ack",
        "command_id": "c1",
        "result": {"ok": True},
    }

    # replayed nonce → not executed again, but still acked so it stops redelivering
    await mc._handle(ws, frame("c2", "N1"))
    assert executed == ["a1"]
    assert ws.acks[-1] == {
        "type": "ack",
        "command_id": "c2",
        "result": {"ok": False, "error_code": "command_rejected"},
    }

    # a nonce older than the ts window is pruned on the next handled command
    mc._seen_nonces["OLD"] = 1_000_000 - cc.TS_WINDOW_MS - 1
    await mc._handle(ws, frame("c3", "N2"))
    assert "OLD" not in mc._seen_nonces
    assert "N2" in mc._seen_nonces


@pytest.mark.asyncio
async def test_duplicate_delivery_is_debug_not_rejection_warning(
    monkeypatch, caplog,
):
    """Expected crash-recovery redelivery must not look like a security fault."""
    monkeypatch.setattr(cc, "load_pairings", lambda: {
        "op": types.SimpleNamespace(operator_root_pubkey="ROOT", server_url="https://s"),
    })
    monkeypatch.setattr(
        cc, "decrypt_command",
        lambda *args: {"op": "pause", "agent_slug": "a1", "params": {}},
    )
    mc = MachineControlClient(machine=object())
    mc._seen_nonces["N1"] = 1
    caplog.set_level(logging.DEBUG, logger=cc.log.name)

    await mc._handle(
        _FakeWS(),
        {
            "command_id": "c2",
            "operator_slug": "op",
            "envelope": {"nonce": "N1", "ts": 1},
        },
    )

    assert "duplicate delivery suppressed" in caplog.text
    assert not any(
        record.levelno >= logging.WARNING and "replayed nonce" in record.message
        for record in caplog.records
    )


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


@pytest.mark.asyncio
async def test_create_delegates_to_control_provisioner(home, monkeypatch):
    seen = {}

    async def fake_provision(params, operator_key, *, preflight, materialize):
        seen.update(
            params=params,
            operator_key=operator_key,
            preflight=preflight,
            materialize=materialize,
        )
        return {"agent_id": "helper-1"}

    monkeypatch.setattr(
        "puffo_agent.portal.control.provision.provision_agent_from_bundle",
        fake_provision,
    )
    params = {
        "pending_token": "pending_1",
        "identity_bundle": {"slug_binding": {}},
        "puffo_core": {"server_url": "browser-placeholder"},
    }
    result = await execute_command(
        "create",
        None,
        params,
        server_url="https://relay.example",
        paired_root_pubkey="operator-key",
    )
    assert result == {"ok": True, "agent_slug": "helper-1"}
    assert seen["operator_key"] == "operator-key"
    assert seen["params"]["puffo_core"]["server_url"] == "https://relay.example"


@pytest.mark.asyncio
async def test_handle_ack_carries_the_exact_command_result(monkeypatch):
    result = {
        "ok": False,
        "error": "Pi sign-in required",
        "error_code": "harness_not_ready",
        "harness": "pi",
        "reason": "need_login",
    }

    async def fake_exec(*args, **kwargs):
        return result

    monkeypatch.setattr(cc, "execute_command", fake_exec)
    monkeypatch.setattr(
        cc,
        "load_pairings",
        lambda: {
            "op": types.SimpleNamespace(
                operator_root_pubkey="ROOT", server_url="https://s"
            )
        },
    )
    monkeypatch.setattr(
        cc,
        "decrypt_command",
        lambda *args: {"op": "create", "agent_slug": None, "params": {}},
    )
    ws = _FakeWS()

    await MachineControlClient(machine=object())._handle(
        ws,
        {"command_id": "create-1", "operator_slug": "op", "envelope": {}},
    )

    assert ws.acks == [
        {"type": "ack", "command_id": "create-1", "result": result}
    ]


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


@pytest.mark.asyncio
async def test_usage_loop_swallows_probe_exception(monkeypatch):
    _wire_usage(monkeypatch, snapshot={}, status=200, calls=[])

    async def _boom(_home):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(cc, "collect_usage_snapshot", _boom)
    # Must not propagate — the loop logs and waits for the next tick.
    await cc.ControlManager()._usage_loop(types.SimpleNamespace(machine_id="mac_1"))


def _wire_run(monkeypatch, mgr, *, pairing_seq, stop_after):
    """Drive ControlManager.run() deterministically: each control task becomes a
    cancellable sleep, and the pairing lookup follows ``pairing_seq``."""
    import asyncio as aio

    usage_spawns = []
    monkeypatch.setattr(cc, "load_pairings", lambda: pairing_seq.pop(0) if pairing_seq else {})
    monkeypatch.setattr(cc, "load_or_create_machine", lambda: types.SimpleNamespace(machine_id="mac_1"))

    class _FakeClient:
        def __init__(self, machine):
            pass

        def run(self, stop):
            return aio.sleep(3600)

    monkeypatch.setattr(cc, "MachineControlClient", _FakeClient)
    monkeypatch.setattr(mgr, "_me_loop", lambda machine: aio.sleep(3600))

    def _usage(machine):
        usage_spawns.append(machine)
        return aio.sleep(3600)

    monkeypatch.setattr(mgr, "_usage_loop", _usage)

    ticks = {"n": 0}

    async def _sos(stop, timeout):
        ticks["n"] += 1
        if ticks["n"] >= stop_after:
            stop.set()

    monkeypatch.setattr(cc, "_sleep_or_stop", _sos)
    return usage_spawns


@pytest.mark.asyncio
async def test_run_spawns_usage_task_and_cancels_on_exit(monkeypatch):
    mgr = cc.ControlManager()
    spawns = _wire_run(monkeypatch, mgr, pairing_seq=[{"op": object()}], stop_after=1)
    await mgr.run()  # exits while still paired → finally cancels the live tasks
    assert len(spawns) == 1


@pytest.mark.asyncio
async def test_run_cancels_usage_task_when_unpaired(monkeypatch):
    mgr = cc.ControlManager()
    # iter1 paired → spawn; iter2 unpaired → the cancel-and-clear branch runs.
    spawns = _wire_run(monkeypatch, mgr, pairing_seq=[{"op": object()}, {}], stop_after=2)
    await mgr.run()
    assert len(spawns) == 1


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


@pytest.mark.asyncio
async def test_edit_applies_inference_level(home):
    # PUF-373: a remote edit (web → linked machine) must apply inference_level
    # onto the runtime block, not silently drop it.
    write_test_agent(home, "scout")
    res = await execute_command("edit", "scout", {"runtime": {"inference_level": "high"}})
    assert res["ok"] is True
    assert AgentConfig.load("scout").runtime.inference_level == "high"


@pytest.mark.asyncio
async def test_edit_rejects_invalid_inference_level(home):
    write_test_agent(home, "scout")
    res = await execute_command("edit", "scout", {"runtime": {"inference_level": "turbo"}})
    assert res["ok"] is False
    assert "inference_level" in res["error"]
    assert AgentConfig.load("scout").runtime.inference_level == ""


@pytest.mark.asyncio
async def test_edit_sets_env_override_threshold(home):
    write_test_agent(home, "scout")
    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}},
    )
    assert res["ok"] is True
    assert AgentConfig.load("scout").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
    }


@pytest.mark.asyncio
async def test_edit_maps_legacy_threshold_key_to_codex(home):
    write_test_agent(home, "scout")
    cfg = AgentConfig.load("scout")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.provider = "openai"
    cfg.runtime.harness = "codex"
    cfg.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}
    cfg.save()

    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"}},
    )

    assert res["ok"] is True
    assert AgentConfig.load("scout").env_overrides == {
        "CODEX_AUTOCOMPACT_PCT_OVERRIDE": "30",
    }


@pytest.mark.asyncio
async def test_edit_prefers_explicit_codex_threshold_key(home):
    write_test_agent(home, "scout")
    cfg = AgentConfig.load("scout")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.provider = "openai"
    cfg.runtime.harness = "codex"
    cfg.save()

    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
            "CODEX_AUTOCOMPACT_PCT_OVERRIDE": "30",
        }},
    )

    assert res["ok"] is True
    assert AgentConfig.load("scout").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
        "CODEX_AUTOCOMPACT_PCT_OVERRIDE": "30",
    }


@pytest.mark.asyncio
async def test_edit_rejects_non_whitelisted_env_key(home):
    write_test_agent(home, "scout")
    res = await execute_command(
        "edit", "scout", {"env_overrides": {"PATH": "/tmp/evil"}},
    )
    assert res["ok"] is False
    assert "not allowed" in res["error"]
    assert AgentConfig.load("scout").env_overrides == {}


@pytest.mark.asyncio
async def test_edit_rejects_threshold_claude_code_would_ignore(home):
    write_test_agent(home, "scout")
    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "0"}},
    )
    assert res["ok"] is False
    assert AgentConfig.load("scout").env_overrides == {}


@pytest.mark.asyncio
async def test_edit_empty_value_clears_the_override(home):
    write_test_agent(home, "scout")
    cfg = AgentConfig.load("scout")
    cfg.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"}
    cfg.save()
    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": ""}},
    )
    assert res["ok"] is True
    assert AgentConfig.load("scout").env_overrides == {}


@pytest.mark.asyncio
async def test_edit_env_overrides_preserves_untouched_fields(home):
    write_test_agent(home, "scout")
    await execute_command("edit", "scout", {"display_name": "Scout One"})
    res = await execute_command(
        "edit", "scout",
        {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}},
    )
    assert res["ok"] is True
    cfg = AgentConfig.load("scout")
    assert cfg.display_name == "Scout One"
    assert cfg.env_overrides == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}


@pytest.mark.asyncio
async def test_concurrent_identical_env_edits_are_idempotent(home):
    import asyncio

    write_test_agent(home, "scout")
    params = {"env_overrides": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}}
    results = await asyncio.gather(
        *(execute_command("edit", "scout", params) for _ in range(8))
    )

    assert all(result["ok"] is True for result in results)
    assert AgentConfig.load("scout").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"
    }


# ── PUF-431: the web "Refresh" button is a full refresh ─────────────
#
# The button sends the control-ws ``refresh`` op with NO params
# (client/web AgentsPane.tsx:380 → portal-store runCommand), so these
# defaults are the whole user-visible contract.


def _cli_local_agent(home, agent_id: str = "scout"):
    from puffo_agent.portal.state import refresh_agent_flag_path

    write_test_agent(home, agent_id)
    cfg = AgentConfig.load(agent_id)
    cfg.runtime.kind = "cli-local"
    cfg.save()
    return cfg.resolve_workspace_dir(), refresh_agent_flag_path


def _flags(workspace):
    from puffo_agent.portal.state import (
        refresh_agent_flag_path,
        refresh_host_sync_flag_path,
        refresh_model_flag_path,
        refresh_session_flag_path,
    )

    return {
        "agent": refresh_agent_flag_path(workspace).exists(),
        "host_sync": refresh_host_sync_flag_path(workspace).exists(),
        "session": refresh_session_flag_path(workspace).exists(),
        "model": refresh_model_flag_path(workspace).exists(),
    }


@pytest.mark.asyncio
async def test_web_refresh_with_no_params_fires_all_three_flags(home):
    """AC #1/#4 — a paramless refresh (what the web sends) drops the
    session, reloads profile.md + memory, and re-syncs host MCP/skills."""
    from puffo_agent.portal.state import restart_flag_path

    workspace, _ = _cli_local_agent(home)

    res = await execute_command("refresh", "scout", {})

    assert res["ok"] is True
    assert sorted(res["touched"]) == [
        "refresh_agent",
        "refresh_host_sync",
        "refresh_session",
    ]
    flags = _flags(workspace)
    assert flags["agent"] and flags["host_sync"] and flags["session"]
    assert not flags["model"]
    # The worker keeps running: no restart.flag means the reconciler
    # never stops it, so the refresh lands at the next turn start.
    assert not restart_flag_path("scout").exists()
    assert AgentConfig.load("scout").state == "running"


@pytest.mark.asyncio
async def test_refresh_still_honors_an_explicitly_narrow_request(home):
    """Defaults changed, the parameter contract didn't — a caller that
    wants prompt-only still gets prompt-only."""
    workspace, _ = _cli_local_agent(home)

    res = await execute_command(
        "refresh", "scout", {"session": False, "host_sync": False}
    )

    assert res["ok"] is True
    assert res["touched"] == ["refresh_agent"]
    flags = _flags(workspace)
    assert flags["agent"]
    assert not flags["session"]
    assert not flags["host_sync"]


@pytest.mark.asyncio
async def test_harness_swap_is_unaffected_by_the_new_defaults(home):
    """The harness/model branch writes refresh_model alone and never
    consulted session/host_sync — flipping their defaults must not leak
    into it."""
    workspace, _ = _cli_local_agent(home)

    res = await execute_command(
        "refresh", "scout", {"harness": "codex", "model": "gpt-5.1-codex-max"}
    )

    assert res["ok"] is True
    assert res["touched"] == ["refresh_model"]
    flags = _flags(workspace)
    assert flags["model"]
    assert not (flags["agent"] or flags["session"] or flags["host_sync"])


@pytest.mark.asyncio
async def test_cli_docker_host_sync_guard_not_tripped_by_defaults(home):
    """The cli-docker guard rejects host_sync without a session drop.
    The new defaults set both, so a docker agent's Refresh still works —
    a session-only default would have broken it."""
    write_test_agent(home, "dock")
    cfg = AgentConfig.load("dock")
    cfg.runtime.kind = "cli-docker"
    cfg.save()

    res = await execute_command("refresh", "dock", {})

    assert res["ok"] is True
    assert sorted(res["touched"]) == [
        "refresh_agent",
        "refresh_host_sync",
        "refresh_session",
    ]


@pytest.mark.asyncio
async def test_refresh_still_rejects_a_non_cli_runtime(home):
    """Unchanged guard — ws-local has no daemon-owned worker to refresh
    (an external tool brings its own engine)."""
    write_test_agent(home, "external")
    cfg = AgentConfig.load("external")
    cfg.runtime.kind = "ws-local"
    cfg.save()

    res = await execute_command("refresh", "external", {})

    assert res["ok"] is False
    assert "cli-local" in res["error"]
