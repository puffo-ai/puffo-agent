"""execute_command applies portal commands to local agent state."""

from __future__ import annotations

import types
import asyncio
import json

import pytest

from _bridge_support import isolated_home, write_test_agent
from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair, hpke_seal
from puffo_agent.portal.control import client as cc
from puffo_agent.portal.control import store as control_store
from puffo_agent.portal.control.client import MachineControlClient, execute_command
from puffo_agent.portal.control.envelope import PORTAL_CMD_INFO
from puffo_agent.portal.state import AgentConfig


class _FakeWS:
    def __init__(self):
        self.acks = []

    async def send_json(self, obj):
        self.acks.append(obj)


class _ControlProcess:
    def __init__(self, on_frame):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.writes = []
        self.stdin = types.SimpleNamespace(
            write=self._write,
            drain=lambda: asyncio.sleep(0),
        )
        self.on_frame = on_frame

    def _write(self, value):
        frame = json.loads(value)
        self.writes.append(frame)
        self.on_frame(frame)

    def feed(self, frame):
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def terminate(self):
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        return int(self.returncode or 0)


def _signed_control_envelope(
    operator, machine, *, command_id, op, params, nonce, ts,
):
    body = json.dumps({"op": op, "params": params}).encode()
    sealed = hpke_seal(
        machine.kem_keypair().public_key_bytes(),
        PORTAL_CMD_INFO,
        command_id.encode(),
        body,
    )
    envelope = {
        "v": 1,
        "command_id": command_id,
        "to_machine_id": machine.machine_id,
        "agent_slug": "control-agent",
        "ts": ts,
        "nonce": nonce,
        "hpke_enc": base64url_encode(sealed.enc),
        "ciphertext": base64url_encode(sealed.ciphertext),
    }
    envelope["signature"] = base64url_encode(
        operator.sign(canonicalize_for_signing(envelope))
    )
    return envelope


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
        lambda env, machine, root, now: {
            "command_id": env["command_id"],
            "op": "pause", "agent_slug": "a1", "params": {},
        },
    )
    monkeypatch.setattr(cc, "now_ms", lambda: 1_000_000)

    mc = MachineControlClient(machine=object())
    ws = _FakeWS()

    def frame(cid, nonce, ts=1_000_000):
        return {"command_id": cid, "operator_slug": "op",
                "envelope": {"command_id": cid, "nonce": nonce, "ts": ts}}

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


@pytest.mark.asyncio
async def test_handle_binds_execution_and_ack_to_signed_command_id(monkeypatch):
    executed = []

    async def _fake_exec(op, slug, params, **kwargs):
        executed.append(kwargs["command_id"])
        return {"ok": True, "delivered": True, "completed": False}

    monkeypatch.setattr(cc, "execute_command", _fake_exec)
    monkeypatch.setattr(cc, "load_pairings", lambda: {
        "op": types.SimpleNamespace(
            operator_root_pubkey="ROOT", server_url="https://s"
        ),
    })
    monkeypatch.setattr(cc, "decrypt_command", lambda *args: {
        "command_id": "signed-command",
        "op": "runtime.cancel_turn",
        "agent_slug": "a1",
        "params": {},
    })
    client = MachineControlClient(machine=object())
    ws = _FakeWS()
    await client._handle(ws, {
        "type": "command", "command_id": "signed-command",
        "operator_slug": "op", "envelope": {},
    })
    assert executed == ["signed-command"]
    assert ws.acks[-1] == {
        "type": "ack", "command_id": "signed-command"
    }

    await client._handle(ws, {
        "type": "command", "command_id": "unsigned-substitution",
        "operator_slug": "op", "envelope": {},
    })
    assert executed == ["signed-command"]
    assert ws.acks[-1] == {
        "type": "ack", "command_id": "signed-command"
    }


def _configure_live_control_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    machine = control_store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    operator_root = base64url_encode(operator.public_key_bytes())
    monkeypatch.setattr(cc, "load_pairings", lambda: {
        "operator": types.SimpleNamespace(
            operator_root_pubkey=operator_root,
            server_url="https://control.invalid",
        )
    })
    now = 1_000_000
    monkeypatch.setattr(cc, "now_ms", lambda: now)
    return machine, operator, now


async def _open_live_codex_turn(tmp_path):
    from puffo_agent.agent.harness.codex_driver import CodexAppServerDriver
    from puffo_agent.agent.harness.driver import (
        RuntimeSpec,
        SessionRef,
        TurnInput,
    )
    from puffo_agent.agent.harness.runtime_manager import RuntimeManager

    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {}})
        elif method == "thread/start":
            proc.feed({
                "id": frame["id"],
                "result": {"thread": {"id": "native-session"}},
            })
        elif method == "turn/start":
            proc.feed({
                "id": frame["id"],
                "result": {"turn": {"id": "native-turn"}},
            })
        elif method == "turn/interrupt":
            proc.feed({"id": frame["id"], "result": {}})

    proc = _ControlProcess(on_frame)
    holder["proc"] = proc
    driver = CodexAppServerDriver(lambda _spec: proc)
    manager = RuntimeManager(
        driver,
        RuntimeSpec(str(tmp_path)),
        agent_id="control-agent",
        session_ref=SessionRef("logical-session"),
    )
    await manager.open()
    started = await manager.start_turn(TurnInput("hello"))
    stream = manager.events()
    permission_task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    proc.feed({
        "id": 900,
        "method": "item/commandExecution/requestApproval",
        "params": {"turnId": "native-turn", "command": "private"},
    })
    permission_event = await asyncio.wait_for(permission_task, timeout=1)
    assert str(
        getattr(permission_event.type, "value", permission_event.type)
    ) == "turn.permission_requested"
    return manager, proc, started, stream, permission_event.data["permission_ref"]


async def _dispatch_live_control_commands(
    *, machine, operator, now, started, stream, permission_ref,
):
    from puffo_agent.portal.control.agent_create import get_registry

    client = MachineControlClient(machine)
    ws = _FakeWS()
    refs = {
        "session_ref": "logical-session",
        "turn_ref": str(started.turn_ref),
    }
    commands = [
        (
            "signed-permission",
            "runtime.resolve_permission",
            {
                **refs,
                "permission_ref": permission_ref,
                "decision": "approved",
            },
        ),
        ("signed-cancel", "runtime.cancel_turn", refs),
    ]
    updated_task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    for index, (command_id, op, params) in enumerate(commands):
        envelope = _signed_control_envelope(
            operator,
            machine,
            command_id=command_id,
            op=op,
            params=params,
            nonce=f"nonce-{index}",
            ts=now,
        )
        await client._handle(ws, {
            "type": "command",
            "command_id": command_id,
            "operator_slug": "operator",
            "envelope": envelope,
        })
        assert ws.acks[-1] == {
            "type": "ack", "command_id": command_id,
        }
        assert get_registry().peek_result(command_id) == {
            "ok": True, "delivered": True, "completed": False,
        }
    return updated_task, ws


async def _assert_live_control_outcomes(
    *, manager, proc, started, updated_task, permission_ref,
):
    from puffo_agent.portal.control.agent_create import get_registry

    updated = await asyncio.wait_for(updated_task, timeout=1)
    assert str(
        getattr(updated.type, "value", updated.type)
    ) == "turn.permission_updated"
    assert updated.data == {
        "permission_ref": permission_ref,
        "state": "approved",
    }
    assert any(
        frame.get("id") == 900
        and frame.get("result") == {"decision": "approved"}
        for frame in proc.writes
    )
    proc.feed({
        "method": "turn/completed",
        "params": {"turn": {"status": "interrupted"}},
    })
    terminal = await manager.wait_terminal(started.turn_ref)
    assert terminal.data["outcome"] == "cancelled"
    assert get_registry().peek_result("signed-cancel")["completed"] is False


@pytest.mark.asyncio
async def test_real_encrypted_cancel_and_permission_reach_live_codex_manager(
    tmp_path, monkeypatch,
):
    machine, operator, now = _configure_live_control_auth(tmp_path, monkeypatch)
    manager, proc, started, stream, permission_ref = await _open_live_codex_turn(
        tmp_path,
    )
    updated_task, _ws = await _dispatch_live_control_commands(
        machine=machine,
        operator=operator,
        now=now,
        started=started,
        stream=stream,
        permission_ref=permission_ref,
    )
    await _assert_live_control_outcomes(
        manager=manager,
        proc=proc,
        started=started,
        updated_task=updated_task,
        permission_ref=permission_ref,
    )
    await manager.close()


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
