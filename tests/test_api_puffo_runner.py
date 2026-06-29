"""api-puffo runner: bridge WS protocol + LLM tool loop.

Spins up an aiohttp test server that speaks the real bridge wire
protocol (BRIDGE-WIRE-PROTOCOL.md) — ``connected`` first frame,
heartbeat-tolerant, ``send`` / ``fetch_pending`` / ``ack`` /
``list_spaces`` handlers — and drives a full end-to-end turn."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent_cloud.bundle import (
    ApiPuffoBundle,
    materialise_agent_dir,
)
from puffo_agent_cloud.cloud_client import (
    BridgeError,
    CloudBridgeClient,
    CloudLlmClient,
)
from puffo_agent_cloud.tools import dispatch_tool


def _isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="puffo-api-puffo-run-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


def _valid_bundle(slug: str, cloud_url: str) -> ApiPuffoBundle:
    return ApiPuffoBundle.from_dict({
        "agent_slug": slug,
        "operator_slug": "user-test",
        "sandbox_token": "sbx_test_xyz",
        "puffo_cloud_server_url": cloud_url,
        "display_name": "Test Bot",
        "role": "tester",
        "role_short": "tester",
        "soul": "I am the api-puffo end-to-end test bot.",
        "avatar_url": "",
        "api_key": "sk-mock",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    })


# ── tools.py shape ───────────────────────────────────────────────


def test_tool_schemas_match_bridge_protocol_surface():
    from puffo_agent_cloud.tools import TOOL_SCHEMAS
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"send_message", "list_spaces"}


# ── Mock bridge ──────────────────────────────────────────────────


class _MockBridgeApp:
    """Minimal bridge that speaks the wire protocol just enough to
    drive the runner. Holds a queue of pending messages + records
    what the client sent."""

    def __init__(self) -> None:
        self.pending_messages: list[dict] = []
        self.spaces_fixture: list[dict] = []
        self.recv_send: list[dict] = []
        self.recv_ack: list[dict] = []
        self.token_seen: str | None = None

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        self.token_seen = request.headers.get("x-sandbox-token")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "connected"})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            kind = frame.get("type", "")
            if kind == "heartbeat":
                continue
            if kind == "send":
                self.recv_send.append(frame)
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
            elif kind == "ack":
                self.recv_ack.append(frame)
                await ws.send_json({
                    "type": "ack_result",
                    "acked": len(frame.get("envelope_ids", [])),
                })
            elif kind == "list_spaces":
                await ws.send_json({
                    "type": "spaces",
                    "spaces": self.spaces_fixture,
                })
        return ws


def _build_app(bridge: _MockBridgeApp, llm_handler=None) -> web.Application:
    app = web.Application()
    app.router.add_get("/v2/cloud-agents/subscribe", bridge.ws_handler)
    if llm_handler is not None:
        app.router.add_post("/v1/llm/complete", llm_handler)
    return app


# ── Bridge client ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_connects_and_sends_x_sandbox_token_header():
    bridge_app = _MockBridgeApp()
    app = _build_app(bridge_app)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx_test_abc", "agent-slug")
        try:
            await c.connect()
        finally:
            await c.close()
    assert bridge_app.token_seen == "sbx_test_abc"


@pytest.mark.asyncio
async def test_bridge_send_send_correlates_ack_via_client_ref():
    bridge_app = _MockBridgeApp()
    app = _build_app(bridge_app)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        # Run the frame consumer in the background; ack correlation
        # happens inside frames() before yielding non-correlated frames.
        consumer = asyncio.create_task(_drain(c))
        try:
            ack = await c.send_send(
                plaintext="hi alice", recipient_slug="alice",
            )
        finally:
            await c.close()
            consumer.cancel()
    assert ack["envelope_id"].startswith("msg_")
    assert ack["devices_queued"] == 2
    assert len(bridge_app.recv_send) == 1
    sent = bridge_app.recv_send[0]
    assert sent["plaintext"] == "hi alice"
    assert sent["recipient_slug"] == "alice"
    assert sent.get("client_ref")


@pytest.mark.asyncio
async def test_bridge_list_spaces_returns_fixture():
    bridge_app = _MockBridgeApp()
    bridge_app.spaces_fixture = [
        {"space_id": "sp_1", "name": "Eng", "channels": [
            {"channel_id": "ch_1", "name": "general"},
        ]},
    ]
    app = _build_app(bridge_app)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        consumer = asyncio.create_task(_drain(c))
        try:
            resp = await c.send_list_spaces()
        finally:
            await c.close()
            consumer.cancel()
    assert resp["spaces"] == bridge_app.spaces_fixture


async def _drain(c: CloudBridgeClient) -> None:
    try:
        async for _ in c.frames():
            pass
    except Exception:
        pass


# ── dispatch_tool ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_tool_send_message_dm():
    bridge_app = _MockBridgeApp()
    app = _build_app(bridge_app)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        c = CloudBridgeClient(url, "sbx", "slug")
        await c.connect()
        consumer = asyncio.create_task(_drain(c))
        try:
            result = await dispatch_tool(c, "send_message", {
                "plaintext": "hi", "recipient_slug": "alice",
            })
        finally:
            await c.close()
            consumer.cancel()
    assert result.startswith("posted msg_")
    assert len(bridge_app.recv_send) == 1


@pytest.mark.asyncio
async def test_dispatch_tool_rejects_mixed_shape():
    c = CloudBridgeClient("http://unused:1", "sbx", "slug")
    # No connect — error fires client-side before any WS frame.
    result = await dispatch_tool(c, "send_message", {
        "plaintext": "hi",
        "recipient_slug": "alice",
        "space_id": "sp_1",
        "channel_id": "ch_1",
    })
    assert result.startswith("error:")
    assert "recipient_slug" in result
    assert "space_id+channel_id" in result


@pytest.mark.asyncio
async def test_dispatch_tool_rejects_missing_route():
    c = CloudBridgeClient("http://unused:1", "sbx", "slug")
    result = await dispatch_tool(c, "send_message", {"plaintext": "hi"})
    assert result.startswith("error:")
    assert "requires" in result


@pytest.mark.asyncio
async def test_dispatch_tool_unknown_name():
    c = CloudBridgeClient("http://unused:1", "sbx", "slug")
    result = await dispatch_tool(c, "nonexistent_tool", {})
    assert result == "error: unknown tool 'nonexistent_tool'"


# ── End-to-end runner with real bridge + canned LLM ─────────────


@pytest.mark.asyncio
async def test_runner_end_to_end_message_llm_tool_ack():
    _isolated_home()
    bridge_app = _MockBridgeApp()
    # Inject a fake DM that will surface during the fetch_pending
    # backfill on connect.
    bridge_app.pending_messages.append({
        "type": "message",
        "envelope_id": "msg_inject_001",
        "sender_slug": "smoker",
        "recipient_slug": "rb-bot",
        "sent_at": 0,
        "plaintext": "hi",
    })

    llm_calls: list[dict] = []

    async def llm_complete(request: web.Request) -> web.Response:
        body = await request.json()
        llm_calls.append(body)
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        last_content = last.get("content")
        if isinstance(last_content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in last_content
        ):
            return web.json_response({
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
            })
        return web.json_response({
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "send_message",
                    "input": {
                        "plaintext": "echo: hi",
                        "recipient_slug": "smoker",
                    },
                },
            ],
        })

    app = _build_app(bridge_app, llm_handler=llm_complete)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        bundle = _valid_bundle(slug="rb-bot", cloud_url=url)
        materialise_agent_dir(bundle)

        from puffo_agent_cloud.runner import ApiPuffoRunner
        stop = asyncio.Event()
        runner = ApiPuffoRunner("rb-bot", stop)

        # Kick the runner; wait until the turn loop has finished
        # both LLM rounds AND the tool send + ack landed, then stop.
        run_task = asyncio.create_task(runner.run())
        for _ in range(80):
            await asyncio.sleep(0.1)
            if (
                len(llm_calls) >= 2
                and bridge_app.recv_send
                and bridge_app.recv_ack
            ):
                break
        stop.set()
        try:
            await asyncio.wait_for(run_task, timeout=5.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    # 2 LLM rounds: initial (tool_use) + post-tool-result (end_turn).
    assert len(llm_calls) == 2
    assert llm_calls[0]["provider"] == "anthropic"
    assert llm_calls[0]["messages"][0]["content"] == "hi"
    # 1 tool send + 1 ack of the injected envelope.
    assert len(bridge_app.recv_send) == 1
    sent = bridge_app.recv_send[0]
    assert sent["plaintext"] == "echo: hi"
    assert sent["recipient_slug"] == "smoker"
    assert any(
        "msg_inject_001" in (a.get("envelope_ids") or [])
        for a in bridge_app.recv_ack
    )
