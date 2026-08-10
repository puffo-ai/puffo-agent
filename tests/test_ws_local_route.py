"""``serve_attached`` end to end: real session + bridge + endpoint, with
only the client / reporter / agent-config / auth faked.

Drives a full attach: handshake → Connected(profile) → a consumer batch
becomes a bundle → tool acks (consumer advances) → tool replies (relayed
to the client) → disconnect tears everything down.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.portal.ws_local import route as route_mod
from puffo_agent.portal.ws_local.auth import AuthedAgent
from puffo_agent.portal.ws_local.bridge import WsLocalBridge
from puffo_agent.portal.ws_local.bundles import Bundle
from puffo_agent.portal.ws_local.hub import AttachPoint, WsLocalHub
from puffo_agent.portal.ws_local.route import serve_attached


class FakeTransport:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self._inbound.get()

    async def close(self) -> None:
        self.closed = True
        self._inbound.put_nowait(None)

    def feed(self, frame: dict) -> None:
        self._inbound.put_nowait(json.dumps(frame))

    def by_type(self, t: str) -> list:
        return [f for f in self.sent if f["type"] == t]


@pytest.mark.asyncio
async def test_ws_local_adapter_requires_exact_read_admission_correlation():
    adapter = route_mod._WsLocalContextAdapter()
    seen = []

    async def callback(event):
        seen.append(event)

    adapter.register_continuation_callback(
        callback,
        "inbox-read-key",
        tool_names=("read_inbox",),
        tool_arguments={"limit": 7},
        correlation_receipt="read-receipt",
    )
    with pytest.raises(RuntimeError, match="correlation failed"):
        await adapter.emit_admission(
            turn_id="turn-1", correlation_key="wrong-receipt"
        )
    assert seen == []
    # WS frames keep their existing opaque-receipt wire shape, but the
    # private bridge handoff must supply the real tool and normalized semantic
    # arguments before a continuation can be admitted.
    with pytest.raises(RuntimeError, match="tool correlation failed"):
        await adapter.emit_admission(
            turn_id="turn-1", correlation_key="read-receipt",
            tool_name="get_thread_history", tool_arguments={"limit": 7},
        )
    with pytest.raises(RuntimeError, match="argument correlation failed"):
        await adapter.emit_admission(
            turn_id="turn-1", correlation_key="read-receipt",
            tool_name="read_inbox", tool_arguments={"limit": 8},
        )
    assert seen == []
    await adapter.emit_admission(
        turn_id="turn-1", correlation_key="read-receipt",
        tool_name="read_inbox", tool_arguments={"limit": 7},
    )
    assert len(seen) == 1
    assert seen[0].planning_cycle_key == "inbox-read-key"
    assert seen[0].provider_session_id == "ws-local:turn-1"
    with pytest.raises(RuntimeError, match="correlation failed"):
        await adapter.emit_admission(
            turn_id="turn-1", correlation_key="read-receipt"
        )


@pytest.mark.asyncio
async def test_ws_local_bridge_records_actual_tool_before_opaque_admission():
    adapter = route_mod._WsLocalContextAdapter()
    seen = []

    async def callback(event):
        seen.append(event)

    adapter.register_continuation_callback(
        callback,
        "held-key",
        correlation_receipt="held-receipt",
        tool_names=("send_message",),
        tool_arguments={"channel": "ch-1", "text": "draft"},
    )
    runtime = type("Runtime", (), {"adapter": adapter})()
    bridge = WsLocalBridge(runtime=runtime)
    bundle = Bundle("bundle", "root", {}, turn_id="turn-1")
    runtime._ws_local_planned = type("Planned", (), {
        "turn_id": "turn-1", "planning_cycle_key": "initial-key",
    })()

    # The public Admitted frame contains only the receipt.  Without the
    # private observed result, it cannot consume the continuation.
    with pytest.raises(RuntimeError, match="tool result unavailable"):
        await bridge.on_admitted(
            bundle, type("Frame", (), {"correlation_key": "held-receipt"})(),
        )

    await bridge.on_tool_result(
        "get_post", {"channel": "ch-1", "text": "draft"},
        {"tool_result_admission": "[puffo:model-visible-read:held-receipt]"},
    )
    with pytest.raises(RuntimeError, match="tool correlation failed"):
        await bridge.on_admitted(
            bundle, type("Frame", (), {"correlation_key": "held-receipt"})(),
        )
    assert seen == []

    await bridge.on_tool_result(
        "send_message", {"channel": "ch-1", "text": "other"},
        {"tool_result_admission": "[puffo:model-visible-read:held-receipt]"},
    )
    with pytest.raises(RuntimeError, match="argument correlation failed"):
        await bridge.on_admitted(
            bundle, type("Frame", (), {"correlation_key": "held-receipt"})(),
        )
    assert seen == []

    await bridge.on_tool_result(
        "send_message", {"channel": "ch-1", "text": "draft"},
        {"tool_result_admission": "[puffo:model-visible-read:held-receipt]"},
    )
    await bridge.on_admitted(
        bundle, type("Frame", (), {"correlation_key": "held-receipt"})(),
    )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_ws_local_bridge_accepts_the_read_tools_admission_receipt_key():
    """BLOCKER 2: two producers, two key names, one bridge consumer.

    ``read_inbox``/``get_post_segment`` return the receipt as
    ``admission_receipt`` while ``stage_held_send_result`` returns
    ``tool_result_admission``. Recognising only the latter left every read
    admission uncorrelatable, which killed the session mid-turn.
    """
    adapter = route_mod._WsLocalContextAdapter()
    seen = []

    async def callback(event):
        seen.append(event)

    adapter.register_continuation_callback(
        callback,
        "inbox-read-key",
        correlation_receipt="read-receipt",
        tool_names=("read_inbox",),
        tool_arguments={"limit": 7},
    )
    runtime = type("Runtime", (), {"adapter": adapter})()
    bridge = WsLocalBridge(runtime=runtime)
    bundle = Bundle("bundle", "root", {}, turn_id="turn-1")
    runtime._ws_local_planned = type("Planned", (), {
        "turn_id": "turn-1", "planning_cycle_key": "initial-key",
    })()

    await bridge.on_tool_result(
        "read_inbox", {"limit": 7},
        {"messages": [], "admission_receipt": "[puffo:model-visible-read:read-receipt]"},
    )
    await bridge.on_admitted(
        bundle, type("Frame", (), {"correlation_key": "read-receipt"})(),
    )
    assert [event.planning_cycle_key for event in seen] == ["inbox-read-key"]


@pytest.mark.asyncio
async def test_ws_local_history_callback_updates_visible_registry_only_after_admission(
    tmp_path,
):
    from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
    from puffo_agent.agent.message_store import (
        MessageStore,
        ProcessingState,
        ReceiptDisposition,
    )

    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    await store.store_receipt(
        {
            "envelope_id": "ws-history",
            "envelope_kind": "channel",
            "sender_slug": "alice",
            "space_id": "sp-1",
            "channel_id": "ch-1",
            "content": "history",
            "content_type": "text/plain",
            "sent_at": 4,
        },
        server_seq=4,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    adapter = route_mod._WsLocalContextAdapter()
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "ws-runtime-turn"
    runtime.active.provider_session_id = "ws-local:turn-1"
    runtime.active.provider_turn_id = "turn-1"
    staged = await runtime.stage_model_visible_read(
        space_id="sp-1", channel_id="ch-1", through_seq=4,
        through_envelope_id="ws-history", tool_name="get_post",
        tool_arguments={"channel": "ch-1", "seq": 4},
        visible_message_ids=["ws-history"],
    )
    receipt = staged["correlation_receipt"]

    # A failed/no-admission result and a wrong receipt leave the continuation
    # unconsumed and all state untouched.
    assert runtime.active.visible_message_ids == []
    with pytest.raises(RuntimeError, match="correlation failed"):
        await adapter.emit_admission(turn_id="turn-1", correlation_key="wrong")
    row = await store.get_message_by_envelope("ws-history")
    assert runtime.active.visible_message_ids == []
    assert runtime.active.message_ids == []
    assert runtime.active.through_by_channel == {}
    assert row.processing_state == ProcessingState.PENDING.value

    await adapter.emit_admission(
        turn_id="turn-1", correlation_key=receipt,
        tool_name="get_post", tool_arguments={"channel": "ch-1", "seq": 4},
    )
    assert runtime.active.visible_message_ids == ["ws-history"]
    assert runtime.active.message_ids == []
    assert runtime.active.through_by_channel == {("sp-1", "ch-1"): 4}
    assert (await store.get_message_by_envelope("ws-history")).processing_state == (
        ProcessingState.PENDING.value
    )
    with pytest.raises(RuntimeError, match="correlation failed"):
        await adapter.emit_admission(turn_id="turn-1", correlation_key=receipt)
    assert runtime.active.visible_message_ids == ["ws-history"]
    assert runtime.active.through_by_channel == {("sp-1", "ch-1"): 4}

    # An admission retained from the old provider turn must not cross into a
    # replacement runtime turn, even if its receipt is otherwise exact.
    stale = await runtime.stage_model_visible_read(
        space_id="sp-1", channel_id="ch-1", through_seq=4,
        through_envelope_id="ws-history", tool_name="get_post",
        tool_arguments={"channel": "ch-1", "seq": 4},
        visible_message_ids=["ws-history"],
    )
    runtime.active.turn_id = "ws-replacement-turn"
    runtime.active.provider_session_id = "ws-local:turn-2"
    with pytest.raises(RuntimeError, match="crossed the active provider turn"):
        await adapter.emit_admission(
            turn_id="turn-2", correlation_key=stale["correlation_receipt"],
            tool_name="get_post", tool_arguments={"channel": "ch-1", "seq": 4},
        )
    assert runtime.active.visible_message_ids == ["ws-history"]
    assert runtime.active.through_by_channel == {("sp-1", "ch-1"): 4}
    assert (await store.get_message_by_envelope("ws-history")).processing_state == (
        ProcessingState.PENDING.value
    )
    await store.close()


class FakeKeystore:
    """Only the one call ``ReminderSync`` makes on construction."""

    def load_or_create_message_backup_dek(self, _slug):
        return b"\x00" * 32


class FakeHttp:
    """Empty reminder snapshot so ``reconcile_snapshot`` completes."""

    keyless = False

    def __init__(self) -> None:
        self.gets: list[str] = []

    async def get(self, path):
        self.gets.append(path)
        return {"occurrences": [], "next_after": None}

    async def post(self, path, body):
        raise RuntimeError(f"unexpected reminder POST {path}: {body}")


class FakeReporter:
    def __init__(self) -> None:
        self.heartbeats = 0
        self.stopped = False

    async def run_heartbeat_loop(self):
        self.heartbeats += 1
        await asyncio.Event().wait()  # runs until cancelled

    def stop(self):
        self.stopped = True

    async def begin_turn(self, message_id):
        return "run_x"

    async def end_turn_batch(self, runs):
        pass


class V1Client:
    """A capability-less peer's client: real store, no injected runtime."""

    def __init__(self, store, tmp_path) -> None:
        self.slug = "puffotest"
        self.device_id = "dev_test"
        self.keystore = FakeKeystore()
        self.http = FakeHttp()
        self.store = store
        self.space_id = None
        self.workspace = str(tmp_path)
        self.global_runtime = None
        self.connected_callbacks: list = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def add_connected_callback(self, callback):
        self.connected_callbacks.append(callback)

    async def listen(self, _on_message):
        # ``listen`` no longer schedules provider work; the owned runtime does.
        self.global_runtime.notify()
        self.started.set()
        await self.release.wait()

    def set_profile(self, *_a, **_kw):
        return None

    async def recover_pending_delivery(self, _envelope_id: str) -> bool:
        return False


class FakeCfg:
    display_name = "Puffo Test"

    def resolve_profile_path(self):
        return "/nonexistent/profile.md"  # OSError → empty profile_md branch

    def resolve_workspace_dir(self):
        return "/tmp/puffo-ws-local-test"


def _v1_hub(client, tmp_path, reporter):
    class V1Cfg(FakeCfg):
        def resolve_workspace_dir(self):
            return str(tmp_path)

    hub = WsLocalHub()
    hub.register(AttachPoint(
        slug="puffotest", agent_id="puffotest-1", agent_cfg=V1Cfg(),
        client=client, reporter=reporter, ack_timeout_s=180.0, ping_interval_s=30.0,
    ))
    return hub


async def _attach_v1(monkeypatch, hub, client):
    """Handshake with no ``capabilities`` — the documented v1 connect."""
    monkeypatch.setattr(
        route_mod, "authenticate_bundle",
        lambda _blob, _pw: AuthedAgent("puffotest-1", "puffotest", "Puffo Test"),
    )
    transport = FakeTransport()
    transport.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    served = asyncio.ensure_future(serve_attached(transport, hub))
    await asyncio.wait_for(client.started.wait(), 2)
    return transport, served


async def _poll(predicate, tries: int = 300):
    for _ in range(tries):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    return await predicate()


@pytest.mark.asyncio
async def test_full_attach_flow_v1_peer_receives_single_root_bundle_and_can_send(
    monkeypatch, tmp_path,
):
    """One capability-less attach, end to end.

    Covers the documented v1 guarantee in both directions: the peer is
    scheduled and delivered a frozen single-root batch off the owned runtime
    (it used to receive nothing at all), its ``read_inbox`` is admitted
    without any ``admitted`` frame (v1 has none), and ``send_message``
    reaches a real SendCoordinator (it used to fail closed with
    ``coordinator_unavailable``).
    """
    import puffo_agent.agent.send_coordinator as coordinator_mod

    from puffo_agent.agent.message_store import ProcessingState

    monkeypatch.setattr(coordinator_mod, "SendCoordinator", _RecordingCoordinator)
    store = await _seed_v1_store(tmp_path)
    client = V1Client(store, tmp_path)
    reporter = FakeReporter()
    hub = _v1_hub(client, tmp_path, reporter)
    transport, served = await _attach_v1(monkeypatch, hub, client)

    connected = transport.by_type("connected")
    assert connected and connected[0]["agent"]["display_name"] == "Puffo Test"
    # F1: the owned runtime — and therefore the send delegate — is built for a
    # peer that advertised nothing.
    assert client.send_delegate is not None
    assert client.send_coordinator is not None

    bundles = await _poll(lambda: _ready(transport.by_type("bundle")))
    bundle = bundles[0]
    # MAJOR 10: v1 gets the frozen single-root shape, not the v2 global
    # bundle, and the scheduled turn's notice rides the v1 message shape.
    assert not bundle["turn_id"]
    assert bundle["channel_meta"] == {
        "channel_id": "", "channel_name": "", "space_id": "",
        "space_name": "", "is_dm": False,
    }
    assert len(bundle["messages"]) == 1
    notice = bundle["messages"][0]
    assert set(notice) == {
        "channel_id", "channel_name", "space_id", "space_name", "is_dm",
        "sender_slug", "sender_display_name", "sender_email", "text",
        "root_id", "attachments", "sender_is_bot", "mentions", "envelope_id",
        "sent_at", "is_visible_to_human",
    }
    assert "<global_inbox_notice>" in notice["text"]
    assert bundle["root_id"] == notice["envelope_id"]

    # Delivery is the model-visible transition for v1 (there is no `admitted`
    # frame), so the runtime's turn is admitted and bound.
    turn_id = await _poll(lambda: _ready(client.global_runtime.active.turn_id))
    assert client.global_runtime.active.provider_turn_id == turn_id

    # ... and the read the notice asks for is admitted off its tool result,
    # so the rows really do become model-visible for this turn.
    transport.feed({"type": "tool_call", "command_id": "cmd_0",
                    "tool": "read_inbox", "params": {}})
    read = await _poll(lambda: _ready(transport.by_type("tool_result")))
    assert read[0]["ok"] is True
    assert "[puffo:model-visible-read:" in read[0]["result"]["admission_receipt"]
    assert client.global_runtime.active.message_ids == ["v1-one"]

    transport.feed({"type": "tool_call", "command_id": "cmd_1",
                    "tool": "send_message",
                    "params": {"channel": "ch", "text": "done"}})
    results = await _poll(
        lambda: _ready([f for f in transport.by_type("tool_result")
                        if f["command_id"] == "cmd_1"])
    )
    assert results[0]["ok"] is True
    assert results[0]["result"]["state"] == "sent"
    assert [
        request.destination for request in client.send_coordinator.sent
    ] == ["ch"]
    assert reporter.heartbeats == 1  # online while attached

    transport.feed({"type": "ack", "bundle_id": bundle["bundle_id"]})
    transport.feed({"type": "end", "bundle_id": bundle["bundle_id"]})
    row = await _poll(lambda: _processed(store, "v1-one"))
    assert row.processing_state == ProcessingState.PROCESSED.value

    # Tool disconnects → session ends → consumer + heartbeat torn down.
    transport._inbound.put_nowait(None)
    await served
    assert reporter.stopped
    assert hub.registry.current("puffotest") is None  # slot freed
    assert client.global_runtime is None
    await store.close()


async def _ready(value):
    return value


async def _processed(store, envelope_id):
    from puffo_agent.agent.message_store import ProcessingState

    row = await store.get_message_by_envelope(envelope_id)
    return row if row.processing_state == ProcessingState.PROCESSED.value else None


async def _seed_v1_store(tmp_path):
    from puffo_agent.agent.message_store import MessageStore

    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    await _store_v1_receipt(store)
    return store


async def _store_v1_receipt(store, envelope_id: str = "v1-one"):
    from puffo_agent.agent.message_store import ReceiptDisposition

    await store.store_receipt(
        {
            "envelope_id": envelope_id, "envelope_kind": "channel",
            "sender_slug": "alice", "space_id": "sp", "channel_id": "ch",
            "content": {"text": "hello v1", "sender_display_name": "Alice"},
            "content_type": "puffo/message+attachments/v1", "sent_at": 1,
        },
        server_seq=1, disposition=ReceiptDisposition.ELIGIBLE, reason="test receipt",
    )


@pytest.mark.asyncio
async def test_ws_local_owned_runtime_shares_the_worker_reminder_sync_wiring(
    monkeypatch, tmp_path,
):
    """F4: the same reminder tools must mean the same durable contract.

    Also pins re-attach hygiene — ``add_connected_callback`` is append-only
    and the client outlives the session.
    """
    import puffo_agent.agent.send_coordinator as coordinator_mod

    from puffo_agent.agent.message_store import MessageStore

    monkeypatch.setattr(coordinator_mod, "SendCoordinator", _RecordingCoordinator)
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client = V1Client(store, tmp_path)
    hub = _v1_hub(client, tmp_path, FakeReporter())

    transport, served = await _attach_v1(monkeypatch, hub, client)
    scheduler = client.global_runtime.reminder_scheduler
    assert scheduler._delivery_authorizer is not None
    assert scheduler._on_lifecycle_committed is not None
    assert len(client.connected_callbacks) == 1
    transport._inbound.put_nowait(None)
    await served

    client.started.clear()
    transport, served = await _attach_v1(monkeypatch, hub, client)
    assert client.global_runtime.reminder_scheduler._delivery_authorizer is not None
    # Re-attach retargets the one relay instead of stacking a second callback.
    assert len(client.connected_callbacks) == 1
    transport._inbound.put_nowait(None)
    await served
    await store.close()


@pytest.mark.asyncio
async def test_unservable_slug_rejected(monkeypatch):
    hub = WsLocalHub()  # nothing registered
    monkeypatch.setattr(
        route_mod, "authenticate_bundle",
        lambda blob, pw: AuthedAgent("ghost-1", "ghost", "Ghost"),
    )
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    await serve_attached(t, hub)
    errors = t.by_type("error")
    assert errors and "not a ws-local agent" in errors[0]["reason"]


class _RecordingCoordinator:
    def __init__(self, **kwargs):
        self.provider_session_id = None
        self.held_recovery_source = kwargs["held_recovery_source"]
        self.http_client = kwargs.get("http_client")
        self.workspace = kwargs.get("workspace")
        self.sent: list = []

    async def send(self, request, *_args, **_kwargs):
        self.sent.append(request)
        return {"state": "sent", "envelope_id": "env_out"}


class _V2Client:
    def __init__(self, store, tmp_path):
        self.slug = "puffotest"
        self.device_id = "dev"
        self.keystore = FakeKeystore()
        self.http = FakeHttp()
        self.store = store
        self.connected_callbacks: list = []
        self.space_id = None
        self.workspace = str(tmp_path)
        self.global_runtime = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.recovered: list[str] = []

    async def listen(self, _on_message):
        from puffo_agent.agent.message_store import ReceiptDisposition

        await self.store.store_receipt(
            {
                "envelope_id": "global-one", "envelope_kind": "channel",
                "sender_slug": "alice", "space_id": "sp", "channel_id": "ch",
                "content": "hello", "content_type": "text/plain", "sent_at": 1,
            },
            server_seq=1, disposition=ReceiptDisposition.ELIGIBLE,
            reason="test receipt",
        )
        self.global_runtime.notify()
        self.started.set()
        await self.release.wait()

    def add_connected_callback(self, callback):
        self.connected_callbacks.append(callback)

    def set_profile(self, *_args, **_kwargs):
        return None

    async def recover_pending_delivery(self, envelope_id: str) -> bool:
        self.recovered.append(envelope_id)
        return True


async def _start_v2_attached(monkeypatch, tmp_path, store):
    import puffo_agent.agent.send_coordinator as coordinator_mod

    monkeypatch.setattr(coordinator_mod, "SendCoordinator", _RecordingCoordinator)
    monkeypatch.setattr(
        route_mod, "_build_tool_dispatch", lambda _point, _runtime=None: {},
    )
    class V2Cfg(FakeCfg):
        def resolve_workspace_dir(self):
            return tmp_path

    client = _V2Client(store, tmp_path)
    reporter = FakeReporter()
    hub = WsLocalHub()
    hub.register(AttachPoint(
        slug="puffotest",
        agent_id="puffotest-1",
        agent_cfg=V2Cfg(),
        client=client,
        reporter=reporter,
        ack_timeout_s=180,
        ping_interval_s=30,
    ))
    monkeypatch.setattr(
        route_mod,
        "authenticate_bundle",
        lambda _blob, _pw: AuthedAgent(
            "puffotest-1", "puffotest", "Puffo Test",
        ),
    )
    transport = FakeTransport()
    transport.feed({
        "type": "connect",
        "bundle": "Yg==",
        "password": "pw",
        "capabilities": ["multi-target-v2", "explicit-admission-v2"],
    })
    served = asyncio.create_task(serve_attached(transport, hub))
    await asyncio.wait_for(client.started.wait(), 1)
    return client, transport, served


async def _wait_for_v2_bundle(transport):
    for _ in range(300):
        bundles = transport.by_type("bundle")
        if bundles:
            return bundles[0]
        await asyncio.sleep(0.02)
    return transport.by_type("bundle")[0]


async def _admit_and_end_v2_bundle(transport, store, bundle):
    from puffo_agent.agent.message_store import ProcessingState

    assert bundle["turn_id"]
    transport.feed({"type": "ack", "bundle_id": bundle["bundle_id"]})
    await asyncio.sleep(0)
    assert [row.envelope_id for row in await store.get_pending()] == ["global-one"]
    transport.feed({
        "type": "admitted",
        "version": 2,
        "bundle_id": bundle["bundle_id"],
        "turn_id": bundle["turn_id"],
    })
    for _ in range(20):
        run = await store.get_turn_run(bundle["turn_id"])
        if run is not None and run.state == ProcessingState.IN_TURN.value:
            break
        await asyncio.sleep(0.01)
    assert run.state == ProcessingState.IN_TURN.value
    transport.feed({"type": "end", "bundle_id": bundle["bundle_id"]})
    for _ in range(20):
        run = await store.get_turn_run(bundle["turn_id"])
        if run.state == ProcessingState.PROCESSED.value:
            break
        await asyncio.sleep(0.01)
    assert run.state == ProcessingState.PROCESSED.value


@pytest.mark.asyncio
async def test_v2_receipt_to_global_bundle_admitted_end_and_owned_runtime_cleanup(
    monkeypatch, tmp_path,
):
    from puffo_agent.agent.message_store import MessageStore

    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client, transport, served = await _start_v2_attached(monkeypatch, tmp_path, store)
    assert await client.global_runtime.held_recovery_source.catchup_pending("late-one")
    assert client.recovered == ["late-one"]
    bundle = await _wait_for_v2_bundle(transport)
    await _admit_and_end_v2_bundle(transport, store, bundle)
    transport._inbound.put_nowait(None)
    await served
    assert client.global_runtime is None
    assert not hasattr(client, "_ws_local_owned_runtime")
    await store.close()


class _DropConnectedTransport(FakeTransport):
    """Authenticates fine, then loses the ``connected`` frame write."""

    async def send(self, raw: str) -> None:
        if json.loads(raw)["type"] == "connected":
            raise RuntimeError("connected frame write dropped")
        await super().send(raw)


@pytest.mark.asyncio
async def test_pre_attach_failure_leaves_the_client_untouched(monkeypatch, tmp_path):
    """F1/B3: a connection that dies before attach owns nothing on the client.

    The owned runtime used to be installed at session construction — before
    ``registry.acquire`` and before the ``connected`` frame — so a connection
    that never attached left a never-started runtime bound to its own dead
    bridge on the long-lived client, which the next connection then reused.
    """
    import puffo_agent.agent.send_coordinator as coordinator_mod

    from puffo_agent.agent.message_store import MessageStore

    monkeypatch.setattr(coordinator_mod, "SendCoordinator", _RecordingCoordinator)
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client = V1Client(store, tmp_path)
    reporter = FakeReporter()
    hub = _v1_hub(client, tmp_path, reporter)
    monkeypatch.setattr(
        route_mod, "authenticate_bundle",
        lambda _blob, _pw: AuthedAgent("puffotest-1", "puffotest", "Puffo Test"),
    )
    transport = _DropConnectedTransport()
    transport.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})

    await serve_attached(transport, hub)

    assert transport.closed
    assert not client.started.is_set()  # the consumer never ran
    assert reporter.heartbeats == 0  # ... and neither did the attach
    assert hub.registry.current("puffotest") is None  # slot freed
    assert client.global_runtime is None
    assert getattr(client, "send_coordinator", None) is None
    assert getattr(client, "send_delegate", None) is None
    assert not hasattr(client, "_ws_local_owned_runtime")
    await store.close()


@pytest.mark.asyncio
async def test_reconnect_binds_turn_dispatch_to_the_new_bridge(monkeypatch, tmp_path):
    """The reconnect contract: a planned turn lands on the live connection.

    Detach unwinds the connection-owned runtime completely, so the reattach
    builds a fresh one whose ``run_turn`` closes over the *new* bridge and
    session rather than the previous connection's orphaned closure.
    """
    import puffo_agent.agent.send_coordinator as coordinator_mod

    from puffo_agent.agent.message_store import MessageStore

    monkeypatch.setattr(coordinator_mod, "SendCoordinator", _RecordingCoordinator)
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client = V1Client(store, tmp_path)
    hub = _v1_hub(client, tmp_path, FakeReporter())

    first, served = await _attach_v1(monkeypatch, hub, client)  # nothing pending
    first_runtime = client.global_runtime
    assert first_runtime is not None
    first._inbound.put_nowait(None)
    await served
    assert client.global_runtime is None
    assert not hasattr(client, "_ws_local_owned_runtime")

    await _store_v1_receipt(store)
    client.started.clear()
    second, served = await _attach_v1(monkeypatch, hub, client)
    assert client.global_runtime is not None
    assert client.global_runtime is not first_runtime

    bundles = await _poll(lambda: _ready(second.by_type("bundle")))
    assert "<global_inbox_notice>" in bundles[0]["messages"][0]["text"]
    assert first.by_type("bundle") == []  # dispatch follows the live session

    second._inbound.put_nowait(None)
    await served
    assert client.global_runtime is None
    assert client.send_coordinator is None
    assert client.send_delegate is None
    assert not hasattr(client, "_ws_local_owned_runtime")
    await store.close()
