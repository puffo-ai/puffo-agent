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
from puffo_agent.portal.state import AgentConfig, RuntimeState


class _FakeWS:
    def __init__(self):
        self.acks = []

    async def send_json(self, obj):
        self.acks.append(obj)


class _StreamingWS(_FakeWS):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return types.SimpleNamespace(
            type=cc.aiohttp.WSMsgType.TEXT,
            data=json.dumps(self.frames.pop(0)),
        )


class _ControlSession:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def ws_connect(self, *args, **kwargs):
        return self.socket


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


@pytest.mark.asyncio
async def test_completed_redelivery_reacks_exact_result_without_reexecution(monkeypatch):
    result = {"ok": False, "error_code": "agent_start_timeout"}
    executions = 0

    async def fake_exec(*args, **kwargs):
        nonlocal executions
        executions += 1
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
        lambda *args: {"op": "create", "agent_slug": "a1", "params": {}},
    )
    monkeypatch.setattr(cc, "now_ms", lambda: 1_000_000)
    client = MachineControlClient(machine=object())
    ws = _FakeWS()
    frame = {
        "command_id": "create-1",
        "operator_slug": "op",
        "envelope": {"nonce": "N1", "ts": 1_000_000},
    }

    await client._handle(ws, frame)
    await client._handle(ws, frame)

    assert executions == 1
    assert ws.acks == [
        {"type": "ack", "command_id": "create-1", "result": result},
        {"type": "ack", "command_id": "create-1", "result": result},
    ]


@pytest.mark.asyncio
async def test_failed_ack_is_warned_and_retried(monkeypatch, caplog):
    class FailingWS:
        async def send_json(self, obj):
            raise ConnectionError("socket closed")

    client = MachineControlClient(machine=object())
    client._active_ws = FailingWS()
    caplog.set_level(logging.WARNING, logger=cc.log.name)

    result = {"ok": True}
    await client._send_ack("create-1", result)

    assert client._pending_acks == {"create-1": result}
    assert "queued for reconnect" in caplog.text

    recovered_ws = _FakeWS()
    client._active_ws = recovered_ws
    await client._flush_pending_acks(recovered_ws)
    assert client._pending_acks == {}
    assert recovered_ws.acks == [
        {"type": "ack", "command_id": "create-1", "result": result}
    ]


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

    async def started(_agent_id):
        return None

    monkeypatch.setattr(cc, "_wait_for_agent_start", started)
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
async def test_create_reports_worker_start_failure(home, monkeypatch):
    async def fake_provision(*args, **kwargs):
        return {"agent_id": "helper-1"}

    async def failed(_agent_id):
        return {
            "error_code": "agent_start_failed",
            "error": "docker run failed",
        }

    monkeypatch.setattr(
        "puffo_agent.portal.control.provision.provision_agent_from_bundle",
        fake_provision,
    )
    monkeypatch.setattr(cc, "_wait_for_agent_start", failed)
    params = {
        "pending_token": "pending_1",
        "identity_bundle": {"slug_binding": {}},
        "puffo_core": {},
    }

    result = await execute_command(
        "create",
        None,
        params,
        server_url="https://relay.example",
        paired_root_pubkey="operator-key",
    )

    assert result == {
        "ok": False,
        "agent_slug": "helper-1",
        "error_code": "agent_start_failed",
        "error": "docker run failed",
    }


@pytest.mark.asyncio
async def test_start_waiter_ignores_starting_then_returns_running(
    home, monkeypatch,
):
    states = iter(
        [
            RuntimeState(status="starting"),
            RuntimeState(status="running"),
        ]
    )
    monkeypatch.setattr(
        RuntimeState,
        "load",
        classmethod(lambda cls, agent_id: next(states)),
    )
    monkeypatch.setattr(cc, "AGENT_START_POLL_SECONDS", 0)

    assert await cc._wait_for_agent_start("helper-1") is None


@pytest.mark.asyncio
async def test_start_waiter_returns_persisted_worker_error(home, monkeypatch):
    monkeypatch.setattr(
        RuntimeState,
        "load",
        classmethod(
            lambda cls, agent_id: RuntimeState(
                status="error",
                error="Docker daemon stopped",
            )
        ),
    )

    assert await cc._wait_for_agent_start("helper-1") == {
        "error_code": "agent_start_failed",
        "error": "Docker daemon stopped",
    }


@pytest.mark.asyncio
async def test_start_waiter_returns_structured_timeout(home, monkeypatch):
    monkeypatch.setattr(cc, "AGENT_START_TIMEOUT_SECONDS", 0)

    assert await cc._wait_for_agent_start("helper-1") == {
        "error_code": "agent_start_timeout",
        "error": (
            "agent worker has not reached running within 0s; it remains "
            "configured and may still finish starting, so check its status "
            "before retrying"
        ),
    }


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


@pytest.mark.asyncio
async def test_create_wait_does_not_block_followup_machine_command(monkeypatch):
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def fake_exec(op, *args, **kwargs):
        if op == "create":
            create_started.set()
            await release_create.wait()
        return {"ok": True, "op": op}

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
        lambda envelope, *args: {
            "op": envelope["op"],
            "agent_slug": envelope.get("agent_slug"),
            "params": {},
        },
    )
    def frame(command_id, nonce, op, agent_slug=None):
        return {
            "type": "command",
            "command_id": command_id,
            "operator_slug": "op",
            "envelope": {
                "nonce": nonce,
                "ts": cc.now_ms(),
                "op": op,
                "agent_slug": agent_slug,
            },
        }

    ws = _StreamingWS([
        {"type": "connected"},
        frame("create-1", "nonce-create", "create"),
        frame("pause-1", "nonce-pause", "pause", "existing-agent"),
    ])
    monkeypatch.setattr(
        cc,
        "create_remote_http_session",
        lambda *args, **kwargs: _ControlSession(ws),
    )
    monkeypatch.setattr(cc.machine_auth, "ws_connect_frame", lambda machine: {})
    monkeypatch.setattr(cc, "build_capabilities", lambda: {})
    client = MachineControlClient(machine=object())

    # Exercise the real receive loop. Removing its background-create dispatch
    # makes this wait forever on the first command and never ack the second.
    await asyncio.wait_for(
        client._connect_once(asyncio.Event()),
        timeout=0.5,
    )
    assert len(client._command_tasks) == 1, ws.acks
    await asyncio.wait_for(create_started.wait(), timeout=0.5)
    acks = [sent for sent in ws.acks if sent.get("type") == "ack"]
    assert acks == [
        {
            "type": "ack",
            "command_id": "pause-1",
            "result": {"ok": True, "op": "pause"},
        }
    ]

    release_create.set()
    await asyncio.gather(*client._command_tasks)
    assert [sent for sent in ws.acks if sent.get("command_id") == "create-1"] == []

    # The create finished after the first socket's context exited. Its result
    # is held and sent on the next authenticated connection, never on the stale
    # socket captured when the command arrived.
    reconnect_ws = _StreamingWS([{"type": "connected"}])
    monkeypatch.setattr(
        cc,
        "create_remote_http_session",
        lambda *args, **kwargs: _ControlSession(reconnect_ws),
    )
    await client._connect_once(asyncio.Event())
    assert reconnect_ws.acks[-1] == {
        "type": "ack",
        "command_id": "create-1",
        "result": {"ok": True, "op": "create"},
    }


@pytest.mark.asyncio
async def test_create_redelivery_after_five_minutes_is_not_reexecuted(monkeypatch):
    """A long first build remains deduped beyond the former five-minute window."""
    clock = [1_000_000]
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    executed = []

    async def fake_exec(op, *args, **kwargs):
        executed.append(op)
        if op == "create":
            create_started.set()
            await release_create.wait()
        return {"ok": True, "op": op}

    monkeypatch.setattr(cc, "execute_command", fake_exec)
    monkeypatch.setattr(cc, "now_ms", lambda: clock[0])
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
        lambda envelope, *args: {
            "op": envelope["op"],
            "agent_slug": envelope.get("agent_slug"),
            "params": {},
        },
    )
    monkeypatch.setattr(cc.machine_auth, "ws_connect_frame", lambda machine: {})
    monkeypatch.setattr(cc, "build_capabilities", lambda: {})

    def frame(command_id, nonce, op):
        return {
            "type": "command",
            "command_id": command_id,
            "operator_slug": "op",
            "envelope": {
                "nonce": nonce,
                "ts": 1_000_000,
                "op": op,
            },
        }

    client = MachineControlClient(machine=object())
    first_ws = _StreamingWS([
        {"type": "connected"},
        frame("create-1", "nonce-create", "create"),
    ])
    monkeypatch.setattr(
        cc,
        "create_remote_http_session",
        lambda *args, **kwargs: _ControlSession(first_ws),
    )
    await client._connect_once(asyncio.Event())
    await asyncio.wait_for(create_started.wait(), timeout=0.5)

    # A different command forces replay-cache pruning after five minutes,
    # followed by server redelivery of the still-running create.
    clock[0] += 5 * 60 * 1000 + 1
    second_ws = _StreamingWS([
        {"type": "connected"},
        frame("pause-1", "nonce-pause", "pause"),
        frame("create-1", "nonce-create", "create"),
    ])
    monkeypatch.setattr(
        cc,
        "create_remote_http_session",
        lambda *args, **kwargs: _ControlSession(second_ws),
    )
    await client._connect_once(asyncio.Event())
    assert executed == ["create", "pause"]
    assert not any(sent.get("command_id") == "create-1" for sent in second_ws.acks)

    release_create.set()
    await asyncio.gather(*client._command_tasks)
    assert cc.TS_WINDOW_MS >= cc.AGENT_START_TIMEOUT_SECONDS * 1000


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
