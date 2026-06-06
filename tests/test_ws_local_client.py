"""Integration smoke test for ``puffo-agent ws-local`` reference client.

Stands up a fake WS server that speaks the ws-local protocol, runs
the real ``run_attach`` against it, and verifies the on-disk
events/commands/status surface matches what the SKILL.md promises an
external AI tool will see.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web

from puffo_agent.portal.ws_local.ws_local_client import run_attach


async def _start_fake_daemon(
    fake_handler,
) -> tuple[web.AppRunner, str]:
    """Spin up an aiohttp app that exposes ``/v1/ws-local`` and let the
    test plug a handler in. Returns runner + base url."""
    app = web.Application()
    app.router.add_get("/v1/ws-local", fake_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_happy_path_handshake_bundle_reply_ack_detach(tmp_path: Path):
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"fake-bundle-bytes")

    received: list[dict] = []
    server_done = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            received.append(frame)
            kind = frame.get("type")
            if kind == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected",
                    "session_id": "sess1",
                    "agent": {"slug": "fake-1234", "role": "tester"},
                }))
                await ws.send_str(json.dumps({
                    "type": "bundle",
                    "bundle_id": "b1",
                    "root_id": "msg_root",
                    "channel_meta": {"channel_id": "ch_x"},
                    "messages": [{"text": "hello"}],
                }))
            elif kind == "ack":
                # client acked; close from server side.
                await ws.close()
        server_done.set()
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path,
            "abc12345",
            bridge_url=base,
            session_dir=session_dir,
        ))

        events_path = session_dir / "events.ndjson"
        commands_path = session_dir / "commands.ndjson"

        # Wait for the bundle event to land.
        for _ in range(50):
            if events_path.exists():
                lines = events_path.read_text(encoding="utf-8").splitlines()
                if any(json.loads(line).get("type") == "bundle" for line in lines if line):
                    break
            await asyncio.sleep(0.05)

        bundle_event = next(
            json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("type") == "bundle"
        )
        assert bundle_event["bundle_id"] == "b1"

        # Append an ack command; client should pick it up on poll.
        with commands_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ack", "bundle_id": "b1"}) + "\n")

        await asyncio.wait_for(server_done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)

        assert received[0]["type"] == "connect"
        assert received[0]["password"] == "abc12345"
        assert received[1]["type"] == "ack"
        assert received[1]["bundle_id"] == "b1"

        status = json.loads((session_dir / "status").read_text(encoding="utf-8"))
        assert status["state"] in ("disconnected", "connected")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_tool_call_command_forwards_and_emits_result(tmp_path: Path):
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"x")
    received: list[dict] = []
    server_done = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            received.append(frame)
            if frame["type"] == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected", "session_id": "s", "agent": {},
                }))
            elif frame["type"] == "tool_call":
                await ws.send_str(json.dumps({
                    "type": "tool_result",
                    "command_id": frame["command_id"],
                    "ok": True,
                    "result": "posted ok",
                }))
                await ws.close()
        server_done.set()
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path, "abc12345", bridge_url=base, session_dir=session_dir,
        ))
        commands_path = session_dir / "commands.ndjson"
        for _ in range(50):
            if commands_path.exists():
                break
            await asyncio.sleep(0.05)
        with commands_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "tool_call", "command_id": "c1",
                "tool": "send_message",
                "params": {"channel": "ch_x", "text": "hi"},
            }) + "\n")
        await asyncio.wait_for(server_done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)

        tc = next(f for f in received if f["type"] == "tool_call")
        assert tc["command_id"] == "c1"
        assert tc["tool"] == "send_message"
        assert tc["params"] == {"channel": "ch_x", "text": "hi"}

        events = (session_dir / "events.ndjson").read_text(encoding="utf-8")
        results = [json.loads(l) for l in events.splitlines() if l]
        assert any(
            e.get("type") == "tool_result" and e.get("command_id") == "c1" and e.get("ok") is True
            for e in results
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_tool_call_rejects_non_object_params(tmp_path: Path):
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"x")
    received: list[dict] = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            received.append(frame)
            if frame["type"] == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected", "session_id": "s", "agent": {},
                }))
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path, "abc12345", bridge_url=base, session_dir=session_dir,
        ))
        commands_path = session_dir / "commands.ndjson"
        for _ in range(50):
            if commands_path.exists():
                break
            await asyncio.sleep(0.05)
        with commands_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "tool_call", "command_id": "c1",
                "tool": "send_message", "params": ["not", "an", "object"],
            }) + "\n")
            fh.write(json.dumps({"type": "detach"}) + "\n")
        await asyncio.wait_for(task, timeout=2.0)

        # Server should never see the malformed tool_call — only connect.
        assert [f["type"] for f in received] == ["connect"]
        events = (session_dir / "events.ndjson").read_text(encoding="utf-8")
        assert "tool_call.params must be an object" in events
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bom_prefixed_command_lines_are_accepted(tmp_path: Path):
    """PowerShell ``Add-Content -Encoding UTF8`` writes BOM on every
    append on Windows; the client must strip it before json.loads."""
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"x")
    received: list[dict] = []
    server_done = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            received.append(frame)
            if frame["type"] == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected", "session_id": "s", "agent": {},
                }))
            elif frame["type"] == "ack":
                await ws.close()
        server_done.set()
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path, "abc12345", bridge_url=base, session_dir=session_dir,
        ))
        commands_path = session_dir / "commands.ndjson"
        for _ in range(50):
            if commands_path.exists():
                break
            await asyncio.sleep(0.05)
        # PowerShell-style: BOM as bytes, then a valid JSON ack line.
        with commands_path.open("ab") as fh:
            fh.write(b"\xef\xbb\xbf" + json.dumps({
                "type": "ack", "bundle_id": "b1",
            }).encode("utf-8") + b"\n")
        await asyncio.wait_for(server_done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
        assert any(f["type"] == "ack" and f["bundle_id"] == "b1" for f in received)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_end_command_is_forwarded(tmp_path: Path):
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"x")
    received: list[dict] = []
    server_done = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            received.append(frame)
            if frame["type"] == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected", "session_id": "s", "agent": {},
                }))
            elif frame["type"] == "end":
                await ws.close()
        server_done.set()
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path, "abc12345", bridge_url=base, session_dir=session_dir,
        ))
        commands_path = session_dir / "commands.ndjson"
        for _ in range(50):
            if commands_path.exists():
                break
            await asyncio.sleep(0.05)
        with commands_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "end", "bundle_id": "b1"}) + "\n")
        await asyncio.wait_for(server_done.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
        assert any(f["type"] == "end" and f["bundle_id"] == "b1" for f in received)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_detach_command_closes_ws_and_exits(tmp_path: Path):
    bundle_path = tmp_path / "agent.puffoagent"
    bundle_path.write_bytes(b"x")

    server_seen_close = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame["type"] == "connect":
                await ws.send_str(json.dumps({
                    "type": "connected", "session_id": "s", "agent": {},
                }))
        server_seen_close.set()
        return ws

    runner, base = await _start_fake_daemon(handler)
    try:
        session_dir = tmp_path / "session"
        task = asyncio.create_task(run_attach(
            bundle_path, "abc12345", bridge_url=base, session_dir=session_dir,
        ))

        commands_path = session_dir / "commands.ndjson"
        for _ in range(50):
            if commands_path.exists():
                break
            await asyncio.sleep(0.05)

        with commands_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "detach"}) + "\n")

        await asyncio.wait_for(server_seen_close.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_missing_bundle_path_returns_error(tmp_path: Path):
    missing = tmp_path / "missing.puffoagent"
    rc = await run_attach(missing, "abc12345", bridge_url="http://127.0.0.1:1")
    assert rc == 2
