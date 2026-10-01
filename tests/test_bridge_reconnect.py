"""Bridge reconnect: liveness detection in ``CloudBridgeClient.frames()`` and
the reconnect schedule in ``listen_bridge``.

Background (measured on staging/prod): after an E2B resume the bridge WS
reconnected only on its own schedule (19–21 s after resume), and a silently
dropped socket left messages waiting for the next reconnect. ``frames()`` now
ends the stream on a boot-clock or wall-clock jump across one read slice (the VM
was frozen) or after ``_READ_DEADLINE_SECONDS`` without any inbound frame; ``listen_bridge``
reconnects immediately after a healthy connection is lost, then backs off with
jitter from 0.5 s capped at 5 s.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import puffo_agent.agent.bridge_client as bc_mod
import puffo_agent.agent.bridge_transport as bt_mod
from puffo_agent.agent.bridge_client import CloudBridgeClient

from .test_cloud_bridge_msgflow import FakeBridge, _bridge_client


# ── reconnect schedule ────────────────────────────────────────────────────


def test_first_retry_after_a_loss_is_immediate():
    assert bt_mod.reconnect_delay(0) == 0.0


@pytest.mark.parametrize("attempt", range(1, 12))
def test_backoff_is_jittered_exponential_within_bounds(attempt):
    lo = bt_mod.reconnect_delay(attempt, rand=lambda: 0.0)
    hi = bt_mod.reconnect_delay(attempt, rand=lambda: 0.999999)
    ceiling = min(5.0, 0.5 * 2 ** (attempt - 1))
    assert lo == pytest.approx(ceiling / 2)
    assert ceiling / 2 <= hi <= ceiling <= 5.0
    assert lo > 0


def test_backoff_caps_at_five_seconds():
    assert bt_mod.reconnect_delay(50, rand=lambda: 0.999999) <= 5.0


# ── frames(): liveness against a loopback relay ───────────────────────────


class _Relay:
    """Loopback relay: sends ``connected``, then optional pings every
    ``ping_every`` seconds, else stays silent (a dead-but-open socket)."""

    def __init__(self, ping_every: float | None = None) -> None:
        self.ping_every = ping_every

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "connected"})
        try:
            while not ws.closed:
                if self.ping_every is None:
                    msg = await ws.receive()
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                else:
                    await asyncio.sleep(self.ping_every)
                    await ws.send_json({"type": "ping"})
        except (ConnectionResetError, RuntimeError):
            pass
        return ws


async def _connected_client(relay: _Relay, tc: TestClient) -> CloudBridgeClient:
    url = str(tc.make_url("")).rstrip("/")
    c = CloudBridgeClient(url, "sbx", "slug")
    await c.connect()
    return c


async def _drain(c: CloudBridgeClient) -> list[dict]:
    got = []
    async for f in c.frames():
        got.append(f)
    return got


@pytest.mark.asyncio
async def test_a_wall_clock_jump_ends_the_stream_as_clock_jump(monkeypatch):
    """Simulate an E2B freeze: the wall clock jumps 30 s across one read
    slice. The stream must end at once with cause ``clock_jump``."""
    monkeypatch.setattr(bc_mod, "_WATCH_TICK_SECONDS", 0.05)
    real_time = bc_mod.time.time
    calls = {"n": 0}

    class _Clock:
        monotonic = staticmethod(bc_mod.time.monotonic)

        @staticmethod
        def time() -> float:
            calls["n"] += 1
            # Second read slice "took" 30 s of wall time.
            return real_time() + (30.0 if calls["n"] >= 4 else 0.0)

    relay = _Relay()
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", relay.handler)
    async with TestClient(TestServer(app)) as tc:
        c = await _connected_client(relay, tc)
        monkeypatch.setattr(bc_mod, "time", _Clock)
        await asyncio.wait_for(_drain(c), timeout=2)
        assert c.last_disconnect_cause == bc_mod.CAUSE_CLOCK_JUMP
        assert c.last_clock_jump_s is not None and c.last_clock_jump_s >= 25
        assert c.last_clock_jump_source == "wall"
        await c.close()


@pytest.mark.asyncio
async def test_a_boot_clock_jump_is_detected_and_labelled_boottime(monkeypatch):
    """The NTP-immune signal: CLOCK_BOOTTIME pulls ahead of CLOCK_MONOTONIC
    across one slice while the wall clock does not move."""
    monkeypatch.setattr(bc_mod, "_WATCH_TICK_SECONDS", 0.05)
    drift = {"n": 0}

    def fake_boot_minus_mono() -> float:
        drift["n"] += 1
        # Sampled before and after each slice: calls 1–2 = slice 1, call 3 =
        # before slice 2, call 4 = after it → 40 s suspended DURING slice 2.
        return 0.0 if drift["n"] <= 3 else 40.0

    monkeypatch.setattr(bc_mod, "_boot_minus_mono", fake_boot_minus_mono)
    relay = _Relay()
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", relay.handler)
    async with TestClient(TestServer(app)) as tc:
        c = await _connected_client(relay, tc)
        await asyncio.wait_for(_drain(c), timeout=2)
        assert c.last_disconnect_cause == bc_mod.CAUSE_CLOCK_JUMP
        assert c.last_clock_jump_source == "boottime"
        assert c.last_clock_jump_s == pytest.approx(40.0)
        await c.close()


def test_the_read_deadline_tolerates_one_missed_server_ping():
    # Server pings every 30 s and culls at 90 s: one missed ping must not
    # reconnect, and the client should still notice before the server.
    assert 60.0 < bc_mod._READ_DEADLINE_SECONDS < 90.0


@pytest.mark.asyncio
async def test_a_silent_socket_ends_the_stream_as_read_timeout(monkeypatch):
    """No frame at all (not even the server's ping) for the read deadline:
    the socket is dead even though TCP has not noticed."""
    monkeypatch.setattr(bc_mod, "_WATCH_TICK_SECONDS", 0.05)
    monkeypatch.setattr(bc_mod, "_READ_DEADLINE_SECONDS", 0.3)
    relay = _Relay(ping_every=None)
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", relay.handler)
    async with TestClient(TestServer(app)) as tc:
        c = await _connected_client(relay, tc)
        await asyncio.wait_for(_drain(c), timeout=2)
        assert c.last_disconnect_cause == bc_mod.CAUSE_READ_TIMEOUT
        await c.close()


@pytest.mark.asyncio
async def test_server_pings_keep_the_stream_alive_past_the_deadline(monkeypatch):
    """The server's ping (swallowed, never yielded) re-arms the deadline."""
    monkeypatch.setattr(bc_mod, "_WATCH_TICK_SECONDS", 0.05)
    monkeypatch.setattr(bc_mod, "_READ_DEADLINE_SECONDS", 0.3)
    relay = _Relay(ping_every=0.1)
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", relay.handler)
    async with TestClient(TestServer(app)) as tc:
        c = await _connected_client(relay, tc)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_drain(c), timeout=1.0)  # 3× the deadline
        assert c.last_disconnect_cause is None
        await c.close()


@pytest.mark.asyncio
async def test_consumer_time_on_a_yielded_frame_is_not_mistaken_for_a_freeze(monkeypatch):
    """Only the read is timed: a consumer that takes longer than the jump
    threshold to handle a frame must not trigger ``clock_jump``."""
    monkeypatch.setattr(bc_mod, "_WATCH_TICK_SECONDS", 0.05)
    monkeypatch.setattr(bc_mod, "_CLOCK_JUMP_SECONDS", 0.2)
    monkeypatch.setattr(bc_mod, "_READ_DEADLINE_SECONDS", 5.0)

    class _Pusher:
        async def handler(self, request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "connected"})
            await ws.send_json({"type": "message", "n": 1})
            await ws.send_json({"type": "message", "n": 2})
            await asyncio.sleep(2)
            return ws

    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", _Pusher().handler)
    async with TestClient(TestServer(app)) as tc:
        url = str(tc.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        got = []
        async for f in c.frames():
            got.append(f)
            await asyncio.sleep(0.5)  # slow consumer, > jump threshold
            if len(got) == 2:
                break
        assert [f["n"] for f in got] == [1, 2]
        assert c.last_disconnect_cause is None
        await c.close()


# ── listen_bridge: immediate reconnect, logs, refresh ─────────────────────


class _OneShotBridge(FakeBridge):
    """First connection ends at once with a recorded cause; later ones
    deliver the backfill marker and stay up."""

    def __init__(self, cause: str, jump: float | None = None, **kw):
        super().__init__(**kw)
        self._cause = cause
        self._jump = jump
        self.second_connect = asyncio.Event()
        self.last_disconnect_cause = None
        self.last_clock_jump_s = None

    async def connect(self) -> None:
        self.connect_count += 1
        self.last_disconnect_cause = None
        self.last_clock_jump_s = None
        self.last_clock_jump_source = None
        if self.connect_count >= 2:
            self.second_connect.set()

    async def frames(self):
        yield {"type": "pending_delivered", "count": 0, "more": False}
        if self.connect_count == 1:
            self.last_disconnect_cause = self._cause
            self.last_clock_jump_s = self._jump
            self.last_clock_jump_source = "boottime" if self._jump else None
            return
        await self._blocked.wait()
        yield {}  # pragma: no cover


@pytest.mark.asyncio
async def test_a_detected_freeze_reconnects_immediately_and_logs_both_lines(
    tmp_path, caplog,
):
    bridge = _OneShotBridge(cause="clock_jump", jump=31.7)
    client = _bridge_client(tmp_path, bridge, db="freeze.db")
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(client._listen_bridge())
        await asyncio.wait_for(bridge.second_connect.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert bridge.connect_count == 2
    assert "bridge disconnected cause=clock_jump" in caplog.text
    assert "clock_jump_s=31.7 clock_source=boottime" in caplog.text
    assert "bridge reconnected delay_ms=" in caplog.text
    assert "attempts=1 cause=clock_jump" in caplog.text
    # The first retry after losing a connection is immediate: no backoff line.
    assert "retry_delay_ms=" not in caplog.text
    await client.store.close()


@pytest.mark.asyncio
async def test_every_reconnect_reissues_the_spaces_refresh(tmp_path):
    """The refresh rides the per-connection ``pending_delivered`` marker, so
    a reconnect re-seeds membership missed while the socket was dead."""
    bridge = _OneShotBridge(cause="read_timeout")
    client = _bridge_client(tmp_path, bridge, db="refresh.db")
    task = asyncio.create_task(client._listen_bridge())
    await asyncio.wait_for(bridge.second_connect.wait(), timeout=1)
    for _ in range(50):
        if bridge.list_spaces_count >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.list_spaces_count >= 2  # one per connection
    await client.store.close()
