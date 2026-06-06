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
from typing import Any, Optional

import aiohttp


POLL_INTERVAL_SECONDS = 0.1


async def run_attach(
    bundle_path: Path,
    passcode: str,
    *,
    bridge_url: str = "http://127.0.0.1:63387",
    session_dir: Optional[Path] = None,
) -> int:
    if not bundle_path.is_file():
        print(f"error: bundle not found: {bundle_path}", file=sys.stderr)
        return 2

    bundle_b64 = base64.b64encode(bundle_path.read_bytes()).decode("ascii")

    if session_dir is None:
        suffix = secrets.token_hex(4)
        session_dir = Path(tempfile.gettempdir()) / f"puffo-attach-{suffix}"
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)
    except OSError:
        pass
    events_path = session_dir / "events.ndjson"
    commands_path = session_dir / "commands.ndjson"
    status_path = session_dir / "status"
    commands_path.touch()

    # First stdout line so the wrapping AI can pick the path up.
    print(f"SESSION_DIR={session_dir}", flush=True)

    ws_url = bridge_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/v1/ws-local"

    def emit_event(event: dict[str, Any]) -> None:
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def write_status(state: dict[str, Any]) -> None:
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(status_path)

    write_status({"state": "connecting", "ws_url": ws_url})

    async with aiohttp.ClientSession() as http:
        try:
            ws = await http.ws_connect(ws_url, heartbeat=30.0)
        except Exception as exc:
            emit_event({"type": "error", "reason": f"connect failed: {exc}"})
            write_status({"state": "error", "reason": str(exc)})
            return 1

        await ws.send_str(json.dumps({
            "type": "connect",
            "bundle": bundle_b64,
            "password": passcode,
        }))

        stop = asyncio.Event()

        async def pump_ws() -> None:
            try:
                async for msg in ws:
                    if stop.is_set():
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            frame = json.loads(msg.data)
                        except ValueError:
                            emit_event({"type": "error", "reason": "non-JSON WS frame"})
                            continue
                        emit_event(frame)
                        kind = frame.get("type")
                        if kind == "connected":
                            write_status({"state": "connected", "agent": frame.get("agent", {})})
                        elif kind == "error":
                            write_status({"state": "error", "reason": frame.get("reason", "")})
                            stop.set()
                        elif kind == "ping":
                            await ws.send_str(json.dumps({"type": "pong"}))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        emit_event({"type": "error", "reason": f"ws error: {ws.exception()}"})
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        break
            finally:
                emit_event({"type": "disconnected"})
                write_status({"state": "disconnected"})
                stop.set()

        async def pump_commands() -> None:
            last_offset = 0
            while not stop.is_set():
                try:
                    size = commands_path.stat().st_size
                except FileNotFoundError:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue
                if size > last_offset:
                    with commands_path.open("rb") as fh:
                        fh.seek(last_offset)
                        chunk = fh.read(size - last_offset)
                    last_offset = size
                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            cmd = json.loads(line)
                        except ValueError:
                            emit_event({"type": "error", "reason": f"bad command JSON: {line[:100]}"})
                            continue
                        ctype = cmd.get("type")
                        if ctype == "ack":
                            await ws.send_str(json.dumps({
                                "type": "ack",
                                "bundle_id": str(cmd.get("bundle_id", "")),
                            }))
                        elif ctype == "reply":
                            await ws.send_str(json.dumps({
                                "type": "reply",
                                "channel_id": str(cmd.get("channel_id", "")),
                                "target_root_id": str(cmd.get("target_root_id", "")),
                                "text": str(cmd.get("text", "")),
                            }))
                        elif ctype == "detach":
                            stop.set()
                            await ws.close()
                            return
                        else:
                            emit_event({"type": "error", "reason": f"unknown command type: {ctype!r}"})
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        try:
            await asyncio.gather(pump_ws(), pump_commands())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            if not ws.closed:
                await ws.close()
    return 0
