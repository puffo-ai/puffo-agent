"""T23 phase 1: ``CloudBridgeClient`` vs. a local aiohttp WS server
that speaks the bridge wire protocol (BRIDGE-WIRE-PROTOCOL.md) —
``connected`` first frame, heartbeat-tolerant, ``send`` /
``fetch_pending`` handlers. Loopback only, no real network.
"""

from __future__ import annotations

import asyncio
import json
import uuid

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
                plaintext="hi alice", recipient_slug="alice",
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
    assert sent["client_ref"] == ack["client_ref"]


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
