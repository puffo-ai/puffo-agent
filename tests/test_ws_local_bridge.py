"""Consumer↔tool bridge: dispatch blocks until ack, raises on death.

This is what makes the daemon's serial consumer advance its cursor only
on the tool's ack (return) and preserve it for redelivery on death
(raise). Uses a real ``WsLocalSession`` + ``BundleQueue`` over a fake
transport so the ack/death really flows through the session.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace

import pytest

from puffo_agent.agent.global_inbox_types import ActiveExactUnion
from puffo_agent.portal.ws_local.bridge import BridgeClosed, WsLocalBridge
from puffo_agent.portal.ws_local.bundles import BundleQueue
from puffo_agent.portal.ws_local.session import WsLocalSession


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

    def feed_close(self) -> None:
        self._inbound.put_nowait(None)

    def bundles(self) -> list:
        return [f for f in self.sent if f["type"] == "bundle"]


class FakeReporter:
    async def begin_turn(self, message_id):
        return "run_x"

    async def end_turn_batch(self, runs):
        pass


async def _never(_d):
    await asyncio.Event().wait()


def _session(transport, bridge, *, replies=None):
    seq = itertools.count(1)

    async def send_message(channel: str = "", text: str = "",
                           target_root_id: str = "", **_kw):
        if replies is not None:
            replies.append((channel, target_root_id, text))
        return "ok"

    return WsLocalSession(
        slug="alice",
        session_id="s1",
        transport=transport,
        queue=BundleQueue(make_id=lambda: f"bdl_{next(seq)}"),
        reporter=FakeReporter(),
        tool_dispatch={"send_message": send_message},
        on_acked=bridge.on_acked,
        on_dead=bridge.on_dead,
        now=lambda: 0.0,
        ack_timeout_s=5.0,
        ping_interval_s=10.0,
        sleep=_never,
    )


def _msg(eid):
    return {"envelope_id": eid, "text": eid}


@pytest.mark.asyncio
async def test_dispatch_returns_after_ack():
    t = FakeTransport()
    bridge = WsLocalBridge()
    sess = _session(t, bridge)
    run = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)

    dispatch = asyncio.ensure_future(
        bridge.dispatch(sess, "r1", [_msg("a")], {"channel_id": "c"})
    )
    await asyncio.sleep(0)
    assert not dispatch.done(), "must block until ack"
    assert t.bundles()[0]["bundle_id"] == "bdl_1"

    t.feed({"type": "end", "bundle_id": "bdl_1"})
    await dispatch  # returns cleanly → consumer advances cursor

    t.feed_close()
    await run


@pytest.mark.asyncio
async def test_dispatch_raises_when_session_dies_before_ack():
    t = FakeTransport()
    bridge = WsLocalBridge()
    sess = _session(t, bridge)
    run = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)

    dispatch = asyncio.ensure_future(
        bridge.dispatch(sess, "r1", [_msg("a")], {"channel_id": "c"})
    )
    await asyncio.sleep(0)
    # Connection drops before the tool acks → session dies → dispatch raises.
    t.feed_close()
    with pytest.raises(BridgeClosed):
        await dispatch
    await run


@pytest.mark.asyncio
async def test_dispatch_after_death_raises_immediately():
    t = FakeTransport()
    bridge = WsLocalBridge()
    sess = _session(t, bridge)
    run = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed_close()
    await run
    with pytest.raises(BridgeClosed):
        await bridge.dispatch(sess, "r1", [_msg("a")], {"channel_id": "c"})


@pytest.mark.asyncio
async def test_v2_disconnect_finalizes_an_empty_durable_turn():
    calls = []

    class Store:
        async def finalize_empty_turn(self, **kwargs):
            calls.append(("finalize", kwargs))

        async def release_notice_delivery(self, session, message_ids):
            calls.append(("release", session, message_ids))

    active = ActiveExactUnion(
        turn_id="turn-empty",
        notice_message_ids=["pending-1"],
        provider_session_id="provider-1",
    )
    notified = []
    runtime = SimpleNamespace(
        active=active,
        store=Store(),
        notify=lambda: notified.append(True),
        _turn_state_lock=asyncio.Lock(),
    )
    bridge = WsLocalBridge(runtime=runtime)
    bridge._waiter = asyncio.get_running_loop().create_future()

    await bridge.on_dead("connection lost")

    with pytest.raises(BridgeClosed, match="connection lost"):
        await bridge._waiter
    assert calls == [
        ("finalize", {"turn_id": "turn-empty", "state": "requeued"}),
        ("release", "provider-1", ("pending-1",)),
    ]
    assert not active.turn_id
    assert notified == [True]


@pytest.mark.asyncio
async def test_reply_during_dispatch_relayed():
    t = FakeTransport()
    bridge = WsLocalBridge()
    replies: list = []
    sess = _session(t, bridge, replies=replies)
    run = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)

    dispatch = asyncio.ensure_future(
        bridge.dispatch(sess, "r1", [_msg("a")], {"channel_id": "c"})
    )
    await asyncio.sleep(0)
    t.feed({"type": "tool_call", "command_id": "cmd_1", "tool": "send_message",
            "params": {"channel": "c", "target_root_id": "r1", "text": "hi"}})
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    await dispatch
    assert replies == [("c", "r1", "hi")]
    t.feed_close()
    await run


class _V1SessionStub:
    """A capability-less peer: ``deliver_planned`` would raise for it."""

    capabilities = frozenset()

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self.batches: list = []

    async def deliver_planned(self, _planned):
        raise AssertionError("v1 peer must not be handed the v2 global bundle")

    async def deliver_batch(self, root_id, messages, channel_meta):
        self.batches.append((root_id, messages, channel_meta))
        # v1 acks by ending the bundle; the real session does this from its
        # frame loop, so resolve the bridge's waiter the same way.
        await self._bridge.on_acked(None)


class _RecordingAdapter:
    def __init__(self) -> None:
        self.admissions: list = []

    async def emit_admission(self, *, turn_id, correlation_key, **_evidence):
        self.admissions.append((turn_id, correlation_key))


@pytest.mark.asyncio
async def test_dispatch_planned_maps_a_v1_peer_onto_single_root_batches():
    from puffo_agent.agent.global_inbox_types import MessageRoute, PlannedTurn
    from puffo_agent.agent.message_store_models import StoredMessage

    def _item(envelope_id, root=None):
        return StoredMessage(
            envelope_id=envelope_id, envelope_kind="channel", sender_slug="alice",
            channel_id="ch", space_id="sp", recipient_slug=None,
            content_type="puffo/message+attachments/v1",
            content={"text": f"body {envelope_id}", "sender_display_name": "Alice"},
            sent_at=1, received_at=1, thread_root_id=root,
        )

    def _route(envelope_id, root=""):
        return MessageRoute(
            envelope_id=envelope_id, kind="thread" if root else "channel",
            space_id="sp", channel_id="ch", thread_root_id=root,
        )

    planned = PlannedTurn(
        turn_id="turn-v1", planning_cycle_key="cycle-v1",
        message_ids=("root", "reply", "solo"),
        items=(_item("root"), _item("reply", "root"), _item("solo")),
        routes=(_route("root"), _route("reply", "root"), _route("solo")),
        targets=(("channel", "sp", "ch"),), pending_targets=(),
        target_summary="{}", formatted_blocks=(), provider_input="notice",
        formatted_tokens=0, wrapper_overhead_tokens=0, formatted_bytes=0,
        wrapper_overhead_bytes=0,
    )
    adapter = _RecordingAdapter()
    runtime = type("Runtime", (), {"adapter": adapter})()
    bridge = WsLocalBridge(runtime=runtime)
    session = _V1SessionStub(bridge)

    await bridge.dispatch_planned(session, planned)

    assert [root_id for root_id, _batch, _meta in session.batches] == ["root", "solo"]
    assert [
        [message["envelope_id"] for message in batch]
        for _root_id, batch, _meta in session.batches
    ] == [["root", "reply"], ["solo"]]
    assert session.batches[0][2] == {
        "channel_id": "ch", "channel_name": "", "space_id": "sp",
        "space_name": "", "is_dm": False,
    }
    assert session.batches[0][1][1]["root_id"] == "root"
    assert session.batches[0][1][0]["text"] == "body root"
    # Exactly one initial admission, correlated to the planning cycle.
    assert adapter.admissions == [("turn-v1", "cycle-v1")]
