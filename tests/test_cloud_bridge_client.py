"""T23 phase 1: ``CloudBridgeClient`` vs. a local aiohttp WS server
that speaks the bridge wire protocol (BRIDGE-WIRE-PROTOCOL.md) —
``connected`` first frame, heartbeat-tolerant, ``send`` /
``fetch_pending`` handlers. Loopback only, no real network.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import puffo_agent.agent.bridge_client as bridge_client_mod
from puffo_agent.agent.bridge_client import (
    BridgeClosed,
    BridgeError,
    CloudBridgeClient,
)


class _MockBridgeApp:
    """Minimal bridge speaking just enough of the wire protocol to
    drive the client. Records what the client sent."""

    def __init__(self) -> None:
        # Frames the server pushes in response to fetch_pending.
        self.pending_messages: list[dict] = []
        # Extra frames pushed right after 'connected' (e.g. an
        # uncorrelated error for the frames() surfacing test).
        self.push_after_connect: list[dict] = []
        # First frame override — None means the normal 'connected'.
        self.first_frame: dict | None = None
        # When set, 'send' frames get an error reply (echoing
        # client_ref) instead of an ack.
        self.error_on_send: dict | None = None
        self.recv_send: list[dict] = []
        self.recv_heartbeats: list[dict] = []
        self.recv_statuses: list[dict] = []
        self.token_seen: str | None = None
        self.heartbeat_received = asyncio.Event()

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        self.token_seen = request.headers.get("x-sandbox-token")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(self.first_frame or {"type": "connected"})
        for frame in self.push_after_connect:
            await ws.send_json(frame)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            kind = frame.get("type", "")
            if kind == "heartbeat":
                self.recv_heartbeats.append(frame)
                self.heartbeat_received.set()
            elif kind == "status":
                self.recv_statuses.append(frame)
            elif kind == "send":
                self.recv_send.append(frame)
                if self.error_on_send is not None:
                    await ws.send_json({
                        **self.error_on_send,
                        "client_ref": frame.get("client_ref"),
                    })
                else:
                    await ws.send_json({
                        "type": "ack",
                        "client_ref": frame.get("client_ref"),
                        "envelope_id": f"msg_{uuid.uuid4().hex[:8]}",
                        "devices_queued": 2,
                    })
            elif kind == "fetch_pending":
                count = 0
                while self.pending_messages:
                    await ws.send_json(self.pending_messages.pop(0))
                    count += 1
                await ws.send_json({
                    "type": "pending_delivered",
                    "count": count,
                    "more": False,
                })
        return ws


def _build_app(bridge: _MockBridgeApp) -> web.Application:
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", bridge.ws_handler)
    return app


async def _drain(c: CloudBridgeClient) -> None:
    try:
        async for _ in c.frames():
            pass
    except Exception:
        pass


@pytest.mark.asyncio
async def test_handshake_connected_sends_x_sandbox_token_header():
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_test_abc", "agent-slug")
        try:
            await c.connect()
        finally:
            await c.close()
    assert bridge_app.token_seen == "sbx_test_abc"


@pytest.mark.asyncio
async def test_bad_handshake_first_frame_raises_bridge_error():
    bridge_app = _MockBridgeApp()
    bridge_app.first_frame = {"type": "definitely-not-connected"}
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        with pytest.raises(BridgeError) as excinfo:
            await c.connect()
        await c.close()
    assert excinfo.value.code == "HANDSHAKE"


@pytest.mark.asyncio
async def test_send_send_correlates_ack_via_client_ref():
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        # Ack correlation happens inside frames() — run the consumer
        # in the background while send_send awaits its future.
        consumer = asyncio.create_task(_drain(c))
        try:
            ack = await c.send_send(
                plaintext="hi alice",
                recipient_slug="alice",
                client_ref="stable-ref",
            )
        finally:
            await c.close()
            consumer.cancel()
    assert ack["type"] == "ack"
    assert ack["envelope_id"].startswith("msg_")
    assert len(bridge_app.recv_send) == 1
    sent = bridge_app.recv_send[0]
    assert sent["type"] == "send"
    assert sent["plaintext"] == "hi alice"
    assert sent["client_ref"] == ack["client_ref"] == "stable-ref"


@pytest.mark.asyncio
async def test_fetch_pending_yields_message_then_pending_delivered():
    bridge_app = _MockBridgeApp()
    bridge_app.pending_messages = [{
        "type": "message",
        "envelope_id": "env_1",
        "sender_slug": "alice-0001",
        "plaintext": "hello",
    }]
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            await c.send_fetch_pending()
            seen: list[dict] = []
            async for frame in c.frames():
                seen.append(frame)
                if frame.get("type") == "pending_delivered":
                    break
        finally:
            await c.close()
    assert [f["type"] for f in seen] == ["message", "pending_delivered"]
    assert seen[0]["envelope_id"] == "env_1"
    assert seen[1]["count"] == 1


@pytest.mark.asyncio
async def test_heartbeat_frame_reaches_server(monkeypatch):
    monkeypatch.setattr(
        bridge_client_mod, "_HEARTBEAT_INTERVAL_SECONDS", 0.05,
    )
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            await asyncio.wait_for(
                bridge_app.heartbeat_received.wait(), timeout=5.0,
            )
        finally:
            await c.close()
    assert bridge_app.recv_heartbeats[0] == {"type": "heartbeat"}


@pytest.mark.asyncio
async def test_status_frame_carries_bounded_runtime():
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            await c.send_status(
                "busy",
                current_message_id="m" * 1100,
                error_text="e" * 1100,
                runtime={
                    "kind": "cli-local",
                    "provider": "openai",
                    "harness": "codex",
                    "model": "gpt-5",
                    "max_context": 258_400,
                    "auto_compact_threshold_pct": 75,
                    "secret": "drop-me",
                },
            )
            await asyncio.sleep(0)
        finally:
            await c.close()

    sent = bridge_app.recv_statuses[0]
    assert sent["status"] == "busy"
    assert len(sent["current_message_id"]) == 1024
    assert len(sent["error_text"]) == 1024
    assert sent["runtime"] == {
        "kind": "cli-local",
        "provider": "openai",
        "harness": "codex",
        "model": "gpt-5",
        "max_context": 258_400,
        "auto_compact_threshold_pct": 75,
    }


@pytest.mark.asyncio
async def test_status_frame_carries_health_on_the_wire():
    """Bridge agents have no signed heartbeat route, so the status frame
    is the only way `health` (e.g. `drained`) reaches the server's
    `agent_status` row — dropping it leaves keyless agents permanently
    green in the operator UI."""
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            await c.send_status("idle", health="drained")
            await c.send_status("idle", health="h" * 300)
            await c.send_status("idle", health=None)
            await asyncio.sleep(0)
        finally:
            await c.close()

    assert bridge_app.recv_statuses[0]["health"] == "drained"
    assert len(bridge_app.recv_statuses[1]["health"]) == 256
    # No health known → no field, not an empty string the server must parse.
    assert "health" not in bridge_app.recv_statuses[2]


@pytest.mark.asyncio
async def test_status_rejects_invalid_state_and_clears_non_busy_message_id():
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            with pytest.raises(ValueError, match="invalid agent status"):
                await c.send_status("starting")
            await c.send_status("error", current_message_id="must-not-send")
            await asyncio.sleep(0)
        finally:
            await c.close()

    assert "current_message_id" not in bridge_app.recv_statuses[0]


@pytest.mark.asyncio
async def test_connected_callback_runs_after_every_reconnect():
    bridge_app = _MockBridgeApp()
    calls = 0

    async def on_connected():
        nonlocal calls
        calls += 1

    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        c.add_connected_callback(on_connected)
        await c.connect()
        await c.close()
        await c.connect()
        await c.close()

    assert calls == 2


@pytest.mark.asyncio
async def test_connected_callback_failure_does_not_break_bridge():
    bridge_app = _MockBridgeApp()

    async def broken():
        raise RuntimeError("telemetry unavailable")

    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        c.add_connected_callback(broken)
        await c.connect()
        try:
            await c.send_fetch_pending()
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_correlated_error_frame_rejects_send_as_bridge_error():
    bridge_app = _MockBridgeApp()
    bridge_app.error_on_send = {
        "type": "error",
        "code": "NOT_AUTHORIZED",
        "message": "token revoked",
    }
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        consumer = asyncio.create_task(_drain(c))
        try:
            with pytest.raises(BridgeError) as excinfo:
                await c.send_send(plaintext="hi", recipient_slug="alice")
        finally:
            await c.close()
            consumer.cancel()
    assert excinfo.value.code == "NOT_AUTHORIZED"
    assert "token revoked" in excinfo.value.message


@pytest.mark.asyncio
async def test_uncorrelated_error_frame_surfaces_via_frames():
    bridge_app = _MockBridgeApp()
    bridge_app.push_after_connect = [{
        "type": "error",
        "code": "INTERNAL",
        "message": "hiccup",
    }]
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        try:
            frame = await asyncio.wait_for(
                c.frames().__anext__(), timeout=5.0,
            )
        finally:
            await c.close()
    assert frame == {"type": "error", "code": "INTERNAL", "message": "hiccup"}


@pytest.mark.asyncio
async def test_send_after_close_raises_bridge_closed():
    bridge_app = _MockBridgeApp()
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        await c.close()
        with pytest.raises(BridgeClosed):
            await c.send_send(plaintext="hi", recipient_slug="alice")


# --------------------------------------------------------------------------
# F2: connect() closes the ClientSession on EVERY failed-connect path.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f2_connect_closes_session_when_ws_connect_raises(monkeypatch):
    """A connector/OS error from ``ws_connect`` must not leak the session
    — connect() closes it and leaves ``_session is None``."""
    captured: dict = {}

    async def _boom_ws_connect(self, *a, **k):
        captured["session"] = self  # the bound ClientSession
        raise OSError("connection refused")

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _boom_ws_connect)
    c = CloudBridgeClient("http://127.0.0.1:1", "sbx", "slug")

    with pytest.raises(OSError):
        await c.connect()

    assert c._session is None, "failed connect leaked the ClientSession"
    assert captured["session"].closed is True, "session was not closed"
    assert c._ws is None
    assert c._heartbeat_task is None, "no heartbeat should survive a failed connect"


@pytest.mark.asyncio
async def test_f2_connect_closes_session_when_first_receive_times_out(monkeypatch):
    """A ``TimeoutError`` waiting for the first frame closes both the ws
    and the session — no leak past the handshake."""
    captured: dict = {}

    class _BoomWs:
        def __init__(self):
            self.closed = False

        async def receive(self, timeout=None):
            raise asyncio.TimeoutError()

        async def close(self):
            self.closed = True

    boom_ws = _BoomWs()

    async def _ws_connect_then_timeout(self, *a, **k):
        captured["session"] = self
        return boom_ws

    monkeypatch.setattr(
        aiohttp.ClientSession, "ws_connect", _ws_connect_then_timeout,
    )
    c = CloudBridgeClient("http://127.0.0.1:1", "sbx", "slug")

    with pytest.raises(asyncio.TimeoutError):
        await c.connect()

    assert c._session is None
    assert captured["session"].closed is True
    assert boom_ws.closed is True, "the half-open ws was not closed"
    assert c._ws is None


@pytest.mark.asyncio
async def test_f2_bad_handshake_frame_still_closes_session():
    """The pre-existing bad-first-frame path also closes the session
    (regression guard for the F2 rework)."""
    bridge_app = _MockBridgeApp()
    bridge_app.first_frame = {"type": "definitely-not-connected"}
    async with TestClient(TestServer(_build_app(bridge_app))) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        with pytest.raises(BridgeError) as excinfo:
            await c.connect()
    assert excinfo.value.code == "HANDSHAKE"
    assert c._session is None, "bad-handshake path leaked the session"
    assert c._ws is None


# --------------------------------------------------------------------------
# F3: a timed-out send_ack / send_list_spaces waiter is pulled from its
# FIFO so the next real ack_result / spaces frame resolves the next call.
# --------------------------------------------------------------------------


class _ScriptedWs:
    """A minimal ws that ``frames()`` can iterate: fed TEXT frames arrive
    via ``feed``; ``send_json`` records outbound frames. Blocks on an
    empty inbox like a live socket."""

    def __init__(self):
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, frame):
        self.sent.append(frame)

    def feed(self, frame: dict) -> None:
        self._inbox.put_nowait(
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(frame)),
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._inbox.get()

    async def close(self):
        self.closed = True


async def _client_with_scripted_ws() -> tuple[CloudBridgeClient, _ScriptedWs]:
    c = CloudBridgeClient("http://127.0.0.1:1", "sbx", "slug")
    ws = _ScriptedWs()
    c._ws = ws  # _require_ws returns it (not None, not closed)
    return c, ws


@pytest.mark.asyncio
async def test_f3_send_ack_timeout_discards_waiter_then_next_resolves():
    c, ws = await _client_with_scripted_ws()
    consumer = asyncio.create_task(_drain(c))  # routes ack_result → waiters
    try:
        # 1) No ack_result fed → this ack times out.
        with pytest.raises(asyncio.TimeoutError):
            await c.send_ack(["e1"], timeout=0.05)
        # The dead waiter was pulled out of the FIFO.
        assert c._ack_result_waiters.empty()

        # 2) The NEXT ack must be resolved by the next ack_result — proving
        # the dead waiter isn't eating it.
        async def _deliver():
            while c._ack_result_waiters.empty():
                await asyncio.sleep(0)
            ws.feed({"type": "ack_result", "acked": ["e2"]})

        res, _ = await asyncio.gather(
            c.send_ack(["e2"], timeout=2.0), _deliver(),
        )
        assert res["type"] == "ack_result"
        assert res["acked"] == ["e2"]
        assert c._ack_result_waiters.empty()
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_f3_send_list_spaces_timeout_discards_waiter_then_next_resolves():
    c, ws = await _client_with_scripted_ws()
    consumer = asyncio.create_task(_drain(c))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await c.send_list_spaces(timeout=0.05)
        assert c._spaces_waiters.empty()

        async def _deliver():
            while c._spaces_waiters.empty():
                await asyncio.sleep(0)
            ws.feed({"type": "spaces", "spaces": [{"id": "sp_1"}]})

        res, _ = await asyncio.gather(
            c.send_list_spaces(timeout=2.0), _deliver(),
        )
        assert res["type"] == "spaces"
        assert res["spaces"] == [{"id": "sp_1"}]
        assert c._spaces_waiters.empty()
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


# --------------------------------------------------------------------------
# Keyless invitation bridge contract: list_invites FIFO routing, exact-
# client_ref decide_invitation_result / error routing, timeout removal,
# and close() waiter cleanup with BridgeClosed.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invites_fifo_decision_correlation_and_close_cleanup():
    """Guarded regression: invites resolves the FIFO waiter (never a dead
    timed-out one), decide results/errors route by exact client_ref, and
    close() fails every remaining waiter with BridgeClosed."""
    c, ws = await _client_with_scripted_ws()
    consumer = asyncio.create_task(_drain(c))
    try:
        async def _feed_when(ready, frame):
            while not ready():
                await asyncio.sleep(0)
            ws.feed(frame)

        # A timed-out waiter is pulled out of the FIFO so the next invites
        # frame resolves the next call, not the dead future.
        with pytest.raises(asyncio.TimeoutError):
            await c.send_list_invites(timeout=0.05)
        assert c._invites_waiters.empty()
        res, _ = await asyncio.gather(
            c.send_list_invites(timeout=2.0),
            _feed_when(
                lambda: not c._invites_waiters.empty(),
                {"type": "invites", "invites": [{"invitation_event_id": "iv_1"}]},
            ),
        )
        assert res["invites"] == [{"invitation_event_id": "iv_1"}]

        # A decide result routes by exact client_ref.
        result, _ = await asyncio.gather(
            c.send_decide_invitation(
                invitation_event_id="iv_1", decision="accept", client_ref="dec-1",
            ),
            _feed_when(
                lambda: "dec-1" in c._decide_waiters,
                {"type": "decide_invitation_result", "client_ref": "dec-1",
                 "invitation_event_id": "iv_1", "decision": "accept",
                 "outcome": "applied"},
            ),
        )
        assert result["outcome"] == "applied"
        assert c._decide_waiters == {}

        # A correlated error routes to that waiter as a BridgeError.
        with pytest.raises(BridgeError) as excinfo:
            await asyncio.gather(
                c.send_decide_invitation(
                    invitation_event_id="iv_2", decision="reject", client_ref="dec-2",
                ),
                _feed_when(
                    lambda: "dec-2" in c._decide_waiters,
                    {"type": "error", "code": "NOT_FOUND", "message": "gone",
                     "client_ref": "dec-2"},
                ),
            )
        assert excinfo.value.code == "NOT_FOUND"
        assert c._decide_waiters == {}

        # Validation rejects a bad decision / blank ref before any frame.
        for decision, ref in (("maybe", "dec-3"), ("accept", "  ")):
            with pytest.raises(ValueError):
                await c.send_decide_invitation(
                    invitation_event_id="iv_3", decision=decision, client_ref=ref,
                )

        # close() fails every remaining waiter with a clean BridgeClosed.
        async def _wait_pending():
            await c.send_list_invites(timeout=10.0)

        pending_task = asyncio.create_task(_wait_pending())
        while c._invites_waiters.empty():
            await asyncio.sleep(0)
        await c.close()
        with pytest.raises(BridgeClosed):
            await pending_task
        assert c._invites_waiters.empty()
        assert c._decide_waiters == {}

        # Outbound frames: exactly the four contract keys on decisions and
        # nothing but type on list_invites.
        assert [
            frame for frame in ws.sent if frame.get("type") == "decide_invitation"
        ] == [
            {"type": "decide_invitation", "client_ref": "dec-1",
             "invitation_event_id": "iv_1", "decision": "accept"},
            {"type": "decide_invitation", "client_ref": "dec-2",
             "invitation_event_id": "iv_2", "decision": "reject"},
        ]
        assert all(
            set(frame) == {"type"} for frame in ws.sent
            if frame.get("type") == "list_invites"
        )
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


def test_fresh_tls_context_is_rebuilt_per_call_not_cached():
    """PUF-192: the helper must return a NEW verifying SSLContext each call,
    never a reused singleton — that's what lets a reconnect pick up E2B's
    proxy CA if it landed after the process (and aiohttp's cached default
    context) was created."""
    import ssl as _ssl

    a = bridge_client_mod._fresh_tls_context()
    b = bridge_client_mod._fresh_tls_context()
    assert isinstance(a, _ssl.SSLContext)
    assert a is not b, "must be rebuilt per call, not a cached singleton"
    assert a.verify_mode == _ssl.CERT_REQUIRED, "must be a verifying default context"


@pytest.mark.asyncio
async def test_connect_passes_a_fresh_tls_context_to_ws_connect(monkeypatch):
    """PUF-192: connect() must hand ws_connect an explicit per-connect
    SSLContext, bypassing aiohttp's import-time cached default so a boot-time
    E2B-proxy-CA race self-heals on the next reconnect."""
    import ssl as _ssl

    captured: dict = {}

    async def _capture_ws_connect(self, *a, **k):
        captured["ssl"] = k.get("ssl")
        raise OSError("stop-after-capture")  # we only need the kwarg

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", _capture_ws_connect)
    c = CloudBridgeClient("http://127.0.0.1:1", "sbx", "slug")
    with pytest.raises(OSError):
        await c.connect()
    assert isinstance(captured["ssl"], _ssl.SSLContext), (
        "connect() must pass a fresh SSLContext to ws_connect (PUF-192)"
    )
