"""Reference WS-local attach client.

Run as ``puffo-agent attach <bundle> --passcode <code>``. The process:

1. Reads the ``.puffoagent`` export blob + the matching passcode.
2. Opens a WebSocket to the local daemon's ``/v1/ws-local`` endpoint.
3. Performs the ``connect`` handshake — daemon decrypts the bundle as
   proof of identity.
4. Holds the WS open. Drops every inbound protocol frame as a JSON
   line into ``<session-dir>/events.ndjson``; polls
   ``<session-dir>/commands.ndjson`` ~10 Hz for outbound frames the
   wrapping AI tool wrote.

The on-disk protocol (events / commands / status files in a per-attach
session dir) is the only surface an AI tool needs to consume. See
``skills/use-puffo-agent-attach/SKILL.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

POLL_INTERVAL_SECONDS = 0.1
V2_CAPABILITIES = ("multi-target-v2", "explicit-admission-v2")


async def run_attach(
    bundle_path: Path,
    passcode: str,
    *,
    bridge_url: str = "http://127.0.0.1:63387",
    session_dir: Optional[Path] = None,
) -> int:
    prepared = _prepare_attach(bundle_path, bridge_url, session_dir)
    if prepared is None:
        return 2
    bundle_b64, session_dir, events_path, commands_path, status_path, ws_url = prepared
    print(f"SESSION_DIR={session_dir}", flush=True)
    return await _run_attach_connection(
        bundle_b64, passcode, events_path, commands_path, status_path, ws_url
    )


def _prepare_attach(
    bundle_path: Path, bridge_url: str, session_dir: Path | None
) -> tuple[str, Path, Path, Path, Path, str] | None:
    if not bundle_path.is_file():
        print(f"error: bundle not found: {bundle_path}", file=sys.stderr)
        return None
    if session_dir is None:
        session_dir = (
            Path(tempfile.gettempdir()) / f"puffo-attach-{secrets.token_hex(4)}"
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)
    except OSError:
        pass
    events, commands, status = (
        session_dir / "events.ndjson",
        session_dir / "commands.ndjson",
        session_dir / "status",
    )
    commands.touch()
    url = (
        bridge_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        + "/v1/ws-local"
    )
    return (
        base64.b64encode(bundle_path.read_bytes()).decode("ascii"),
        session_dir,
        events,
        commands,
        status,
        url,
    )


async def _run_attach_connection(
    bundle: str, passcode: str, events: Path, commands: Path, status: Path, url: str
) -> int:
    emit, write_status = _attach_file_writers(events, status)
    write_status({"state": "connecting", "ws_url": url})
    async with aiohttp.ClientSession() as http:
        try:
            ws = await http.ws_connect(url, heartbeat=30.0)
        except Exception as exc:
            emit({"type": "error", "reason": f"connect failed: {exc}"})
            write_status({"state": "error", "reason": str(exc)})
            return 1
        await ws.send_str(
            json.dumps(
                {
                    "type": "connect",
                    "bundle": bundle,
                    "password": passcode,
                    "capabilities": list(V2_CAPABILITIES),
                }
            )
        )
        stop = asyncio.Event()
        try:
            await asyncio.gather(
                _pump_ws(ws, stop, emit, write_status),
                _pump_commands(ws, stop, commands, emit),
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            if not ws.closed:
                await ws.close()
    return 0


def _attach_file_writers(
    events: Path, status: Path
) -> tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]]:
    def emit(event: dict[str, Any]) -> None:
        with events.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def write_state(state: dict[str, Any]) -> None:
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(status)

    return emit, write_state


async def _pump_ws(
    ws: aiohttp.ClientWebSocketResponse,
    stop: asyncio.Event,
    emit: Callable[[dict[str, Any]], None],
    write_status: Callable[[dict[str, Any]], None],
) -> None:
    try:
        async for message in ws:
            if stop.is_set():
                break
            await _handle_ws_message(ws, message, stop, emit, write_status)
    finally:
        emit({"type": "disconnected"})
        write_status({"state": "disconnected"})
        stop.set()


async def _handle_ws_message(
    ws: aiohttp.ClientWebSocketResponse,
    message: aiohttp.WSMessage,
    stop: asyncio.Event,
    emit: Callable[[dict[str, Any]], None],
    write_status: Callable[[dict[str, Any]], None],
) -> None:
    if message.type == aiohttp.WSMsgType.TEXT:
        try:
            frame = json.loads(message.data)
        except ValueError:
            emit({"type": "error", "reason": "non-JSON WS frame"})
            return
        emit(frame)
        kind = frame.get("type")
        if kind == "connected":
            write_status({"state": "connected", "agent": frame.get("agent", {})})
        elif kind == "error":
            write_status({"state": "error", "reason": frame.get("reason", "")})
            stop.set()
        elif kind == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
    elif message.type == aiohttp.WSMsgType.ERROR:
        emit({"type": "error", "reason": f"ws error: {ws.exception()}"})


async def _pump_commands(
    ws: aiohttp.ClientWebSocketResponse,
    stop: asyncio.Event,
    commands: Path,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    offset = 0
    while not stop.is_set():
        try:
            size = commands.stat().st_size
        except FileNotFoundError:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        if size > offset:
            with commands.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
            offset = size
            for line in chunk.decode("utf-8-sig", errors="replace").splitlines():
                if await _send_attach_command(ws, stop, line, emit):
                    return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _send_attach_command(
    ws: aiohttp.ClientWebSocketResponse,
    stop: asyncio.Event,
    line: str,
    emit: Callable[[dict[str, Any]], None],
) -> bool:
    line = line.lstrip("﻿").strip()
    if not line:
        return False
    try:
        command = json.loads(line)
    except ValueError:
        emit({"type": "error", "reason": f"bad command JSON: {line[:100]}"})
        return False
    kind = command.get("type")
    if kind in {"ack", "end"}:
        await ws.send_str(
            json.dumps({"type": kind, "bundle_id": str(command.get("bundle_id", ""))})
        )
        return False
    if kind == "admitted":
        admitted = {
            "type": "admitted",
            "version": 2,
            "bundle_id": str(command.get("bundle_id", "")),
            "turn_id": str(command.get("turn_id", "")),
        }
        correlation_key = command.get("correlation_key")
        if correlation_key is not None:
            admitted["correlation_key"] = str(correlation_key)
        await ws.send_str(json.dumps(admitted))
        return False
    if kind == "tool_call":
        params = command.get("params") or {}
        if not isinstance(params, dict):
            emit({"type": "error", "reason": "tool_call.params must be an object"})
            return False
        await ws.send_str(
            json.dumps(
                {
                    "type": "tool_call",
                    "command_id": str(command.get("command_id", "")),
                    "tool": str(command.get("tool", "")),
                    "params": params,
                }
            )
        )
        return False
    if kind == "detach":
        stop.set()
        await ws.close()
        return True
    emit({"type": "error", "reason": f"unknown command type: {kind!r}"})
    return False
