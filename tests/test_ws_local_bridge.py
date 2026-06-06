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

import pytest

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
