"""Session orchestration, end to end against a fake transport + clock.

Covers the happy turn (bundle → ack → cursor advance + processing
status), replies, ping/pong, malformed frames, the successor-after-ack
sequence, and the unhappy paths: double-ack, unknown-id ack, and a
liveness timeout that rolls the in-flight bundle back.
"""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from puffo_agent.portal.ws_local.bundles import BundleQueue
from puffo_agent.portal.ws_local.session import WsLocalSession


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeTransport:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
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

    def sent_types(self) -> list[str]:
        return [f["type"] for f in self.sent]


class FakeReporter:
    def __init__(self) -> None:
        self.begun: list[str] = []
        self.batches: list[list[dict]] = []

    async def begin_turn(self, message_id: str) -> str:
        self.begun.append(message_id)
        return f"run_{message_id}"

    async def end_turn_batch(self, runs: list[dict]) -> None:
        self.batches.append(runs)


async def _never(_d: float) -> None:
    await asyncio.Event().wait()  # watchdog parked; never times out


def _msg(eid: str) -> dict:
    return {"envelope_id": eid, "text": eid}


def _make_session(transport, queue, reporter, *, acked, replies,
                  now=lambda: 0.0, ack_timeout_s=5.0, ping_interval_s=10.0,
                  sleep=_never, tool_dispatch=None):
    async def on_acked(bundle):
        acked.append(bundle)

    if tool_dispatch is None:
        async def _send_message(channel: str = "", text: str = "",
                                target_root_id: str = "",
                                is_visible_to_human: bool = True,
                                root_id: str = ""):
            replies.append((channel, root_id or target_root_id, text))
            return "ok"
        tool_dispatch = {"send_message": _send_message}

    return WsLocalSession(
        slug="alice",
        session_id="sess_1",
        transport=transport,
        queue=queue,
        reporter=reporter,
        tool_dispatch=tool_dispatch,
        on_acked=on_acked,
        now=now,
        ack_timeout_s=ack_timeout_s,
        ping_interval_s=ping_interval_s,
        sleep=sleep,
        make_run_id=lambda: "run_x",
    )


def _counter_queue() -> BundleQueue:
    seq = itertools.count(1)
    return BundleQueue(make_id=lambda: f"bdl_{next(seq)}")


# ── happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_send_does_not_call_begin_turn():
    """Status stays unchanged until the tool acks. ``_pump`` no longer
    fires ``begin_turn`` so the operator's UI only flips to working_on
    after the AI signals it has started, not just because the daemon
    queued the bundle."""
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})

    assert t.sent_types() == ["bundle"]
    assert r.begun == []
    t.feed_close()
    await task


@pytest.mark.asyncio
async def test_ack_flips_status_without_advancing():
    """Ack mints the run_id but does NOT close the turn or advance
    the cursor — that's reserved for ``end``."""
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked = []
    sess = _make_session(t, q, r, acked=acked, replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})

    t.feed({"type": "ack", "bundle_id": "bdl_1"})
    t.feed_close()
    await task

    assert r.begun == ["a"]
    assert r.batches == []
    assert acked == []
    assert q.has_inflight is False  # rolled back on close, not advanced


@pytest.mark.asyncio
async def test_end_advances_cursor_and_closes_turn():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked = []
    sess = _make_session(t, q, r, acked=acked, replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})

    t.feed({"type": "ack", "bundle_id": "bdl_1"})
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed_close()
    await task

    assert len(acked) == 1 and acked[0].bundle_id == "bdl_1"
    assert r.batches == [[{"run_id": "run_a", "message_id": "a", "succeeded": True}]]
    assert not q.has_inflight
    assert t.closed


@pytest.mark.asyncio
async def test_end_without_ack_still_advances():
    """Skipping ack is valid — an agent that decides not to reply can
    jump straight to ``end``. The session mints a run_id inline so
    the turn record stays complete."""
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked = []
    sess = _make_session(t, q, r, acked=acked, replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})

    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed_close()
    await task

    assert r.begun == ["a"]
    assert len(r.batches) == 1
    assert len(acked) == 1


@pytest.mark.asyncio
async def test_multi_message_bundle_runs_first_from_begin_turn():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    # A whole root batch arrives together → one bundle.
    await sess.deliver_batch("r1", [_msg("a"), _msg("b")], {"channel_id": "c"})

    t.feed({"type": "ack", "bundle_id": "bdl_1"})
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed_close()
    await task

    runs = r.batches[0]
    assert [x["message_id"] for x in runs] == ["a", "b"]
    assert runs[0]["run_id"] == "run_a"      # reuses begin_turn id from ack
    assert runs[1]["run_id"] == "run_x"      # fresh id for the rest


# ── replies + ping ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_frame_relayed_to_sender():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)

    t.feed({"type": "tool_call", "command_id": "cmd_1", "tool": "send_message",
            "params": {"channel": "c", "target_root_id": "r1", "text": "hi"}})
    t.feed_close()
    await task
    assert replies == [("c", "r1", "hi")]


@pytest.mark.asyncio
async def test_tool_call_unknown_tool_emits_error_result():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed({"type": "tool_call", "command_id": "c1", "tool": "made_up", "params": {}})
    t.feed_close()
    await task
    results = [f for f in t.sent if f["type"] == "tool_result"]
    assert results and results[0] == {
        "type": "tool_result", "command_id": "c1", "ok": False,
        "error": "unknown tool: 'made_up'",
    }


@pytest.mark.asyncio
async def test_tool_call_handler_exception_surfaces_as_error_result():
    async def boom(**_kw):
        raise RuntimeError("kaboom")
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[],
                        tool_dispatch={"send_message": boom})
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed({"type": "tool_call", "command_id": "c2",
            "tool": "send_message", "params": {"x": 1}})
    t.feed_close()
    await task
    results = [f for f in t.sent if f["type"] == "tool_result"]
    assert results and results[0]["ok"] is False and results[0]["error"] == "kaboom"


@pytest.mark.asyncio
async def test_tool_call_handler_result_is_returned():
    seen: dict = {}
    async def echo(**params):
        seen.update(params)
        return "echoed"
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[],
                        tool_dispatch={"send_message": echo})
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed({"type": "tool_call", "command_id": "c3",
            "tool": "send_message",
            "params": {"channel": "ch_x", "text": "hi", "extra": True}})
    t.feed_close()
    await task
    results = [f for f in t.sent if f["type"] == "tool_result"]
    assert results[0]["ok"] is True
    assert results[0]["result"] == "echoed"
    assert seen == {"channel": "ch_x", "text": "hi", "extra": True}


@pytest.mark.asyncio
async def test_ping_gets_pong():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed({"type": "ping"})
    t.feed_close()
    await task
    assert "pong" in t.sent_types()


@pytest.mark.asyncio
async def test_malformed_frame_ignored_then_continues():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t._inbound.put_nowait("{bad json")          # ignored
    t.feed({"type": "tool_call", "command_id": "cmd_2", "tool": "send_message",
            "params": {"channel": "c", "text": "ok"}})
    t.feed_close()
    await task
    assert replies == [("c", "", "ok")]


# ── unhappy / races ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_double_end_advances_only_once():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed({"type": "end", "bundle_id": "bdl_1"})  # duplicate end
    t.feed_close()
    await task
    assert len(acked) == 1
    assert len(r.batches) == 1


@pytest.mark.asyncio
async def test_double_ack_is_idempotent():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})
    t.feed({"type": "ack", "bundle_id": "bdl_1"})
    t.feed({"type": "ack", "bundle_id": "bdl_1"})  # duplicate ack
    t.feed_close()
    await task
    # Only one begin_turn — duplicate ack didn't re-mint a run_id.
    assert r.begun == ["a"]


@pytest.mark.asyncio
async def test_ack_after_end_is_noop():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked = []
    sess = _make_session(t, q, r, acked=acked, replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed({"type": "ack", "bundle_id": "bdl_1"})  # stale ack after end
    t.feed_close()
    await task
    assert len(acked) == 1


@pytest.mark.asyncio
async def test_unknown_end_id_is_ignored():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})
    t.feed({"type": "end", "bundle_id": "bdl_nope"})
    t.feed_close()
    await task
    assert acked == []
    assert q.has_inflight is False  # rolled back on close, not advanced


@pytest.mark.asyncio
async def test_successor_sent_only_after_first_ended():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})
    await sess.deliver("r1", _msg("b"), {"channel_id": "c"})  # mid-flight successor

    # Only the first bundle is on the wire; the successor waits.
    bundles = [f for f in t.sent if f["type"] == "bundle"]
    assert len(bundles) == 1 and bundles[0]["messages"][0]["envelope_id"] == "a"

    # End the first → successor (bdl_2) goes out; end it too, then close.
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed({"type": "end", "bundle_id": "bdl_2"})
    t.feed_close()
    await task

    bundles = [f for f in t.sent if f["type"] == "bundle"]
    assert [b["bundle_id"] for b in bundles] == ["bdl_1", "bdl_2"]
    assert bundles[1]["messages"][0]["envelope_id"] == "b"
    assert [b.bundle_id for b in acked] == ["bdl_1", "bdl_2"]


@pytest.mark.asyncio
async def test_pong_frame_is_accepted_silently():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[])
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    t.feed({"type": "pong"})        # keepalive reply, no output expected
    t.feed_close()
    await task
    assert t.sent_types() == []


@pytest.mark.asyncio
async def test_runs_skip_messages_without_envelope_id():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []
    sess = _make_session(t, q, r, acked=acked, replies=replies)
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver_batch(
        "r1", [{"envelope_id": "", "text": "noid"}, _msg("b")], {"channel_id": "c"},
    )
    t.feed({"type": "end", "bundle_id": "bdl_1"})
    t.feed_close()
    await task
    # Empty-id message carries no server row → excluded from runs.
    assert [x["message_id"] for x in r.batches[0]] == ["b"]


def test_alive_true_on_fresh_session():
    sess = _make_session(FakeTransport(), _counter_queue(), FakeReporter(),
                         acked=[], replies=[])
    assert sess.alive is True


# ── watchdog unit ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_tick_sends_ping_when_alive():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[], now=lambda: 0.0, ack_timeout_s=100.0)
    sess._last_rx = 0.0
    assert await sess._watchdog_tick() is True
    assert t.sent_types() == ["ping"]


@pytest.mark.asyncio
async def test_watchdog_tick_dies_on_timeout():
    clock = {"t": 0.0}
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[],
                         now=lambda: clock["t"], ack_timeout_s=5.0)
    sess._last_rx = 0.0
    clock["t"] = 10.0
    assert await sess._watchdog_tick() is False
    assert sess.alive is False
    assert sess._death_reason == "liveness timeout"


@pytest.mark.asyncio
async def test_watchdog_tick_stops_when_already_dead():
    sess = _make_session(FakeTransport(), _counter_queue(), FakeReporter(),
                         acked=[], replies=[])
    await sess._die("x")
    assert await sess._watchdog_tick() is False


@pytest.mark.asyncio
async def test_watchdog_tick_dies_when_ping_send_fails():
    class BoomTransport(FakeTransport):
        async def send(self, raw):
            raise ConnectionResetError("pipe gone")

    t, q, r = BoomTransport(), _counter_queue(), FakeReporter()
    sess = _make_session(t, q, r, acked=[], replies=[], now=lambda: 0.0, ack_timeout_s=100.0)
    sess._last_rx = 0.0
    assert await sess._watchdog_tick() is False
    assert sess._death_reason == "send failed"


@pytest.mark.asyncio
async def test_die_swallows_on_dead_error():
    sess = _make_session(FakeTransport(), _counter_queue(), FakeReporter(),
                         acked=[], replies=[])

    async def boom(_reason):
        raise RuntimeError("hook blew up")

    sess._on_dead = boom
    assert await sess._die("boom") == "boom"
    assert sess.alive is False


@pytest.mark.asyncio
async def test_die_swallows_close_error():
    class BadClose(FakeTransport):
        async def close(self):
            raise OSError("already closed")

    sess = _make_session(BadClose(), _counter_queue(), FakeReporter(),
                         acked=[], replies=[])
    assert await sess._die("boom") == "boom"
    assert sess.alive is False


@pytest.mark.asyncio
async def test_liveness_timeout_rolls_back_inflight():
    t, q, r = FakeTransport(), _counter_queue(), FakeReporter()
    acked, replies = [], []

    clock = {"t": 0.0}

    async def fast_sleep(d):
        clock["t"] += d
        await asyncio.sleep(0)

    sess = _make_session(
        t, q, r, acked=acked, replies=replies,
        now=lambda: clock["t"], ack_timeout_s=5.0, ping_interval_s=10.0,
        sleep=fast_sleep,
    )
    task = asyncio.ensure_future(sess.run())
    await asyncio.sleep(0)
    await sess.deliver("r1", _msg("a"), {"channel_id": "c"})  # in-flight, never acked
    assert q.has_inflight

    reason = await task
    assert reason == "liveness timeout"
    assert t.closed
    assert not q.has_inflight
    assert q.pending_count() == 1  # rolled back to pending for the next session
    assert acked == []
