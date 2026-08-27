"""Reference WS-local attach client.

Run as ``puffo-agent ws-local <bundle> --passcode <code>``. The process:

1. Reads the ``.puffoagent`` export blob + the matching passcode.
2. Opens a WebSocket to the local daemon's ``/v1/ws-local`` endpoint.
3. Performs the ``connect`` handshake: the daemon decrypts the bundle and
   binds its root identity to the locally managed Agent.
4. Holds the WS open. Drops every inbound protocol frame as a JSON
   line into ``<session-dir>/events.ndjson``; polls
   ``<session-dir>/commands.ndjson`` ~10 Hz for outbound frames the
   wrapping AI tool wrote.

The on-disk protocol (events / commands / status files in a per-attach
session dir) is the only surface an AI tool needs to consume. See
``skills/use-puffo-agent-ws-local/SKILL.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

POLL_INTERVAL_SECONDS = 0.1
V2_CAPABILITIES = ("multi-target-v2", "explicit-admission-v2")


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise OSError("short protocol-file write")
        written += count


@dataclass
class _SessionFiles:
    directory: Path
    directory_fd: int | None
    events_fd: int
    commands_fd: int

    def close(self) -> None:
        for fd in (self.events_fd, self.commands_fd, self.directory_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def emit(self, event: dict[str, Any]) -> None:
        _write_all(self.events_fd, (json.dumps(event) + "\n").encode("utf-8"))

    def write_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state).encode("utf-8")
        if self.directory_fd is not None:
            self._write_state_at(payload)
            return
        fd, tmp_name = tempfile.mkstemp(
            prefix=".status.", suffix=".tmp", dir=self.directory
        )
        tmp = Path(tmp_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            _write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, self.directory / "status")
        finally:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)

    def _write_state_at(self, payload: bytes) -> None:
        assert self.directory_fd is not None
        tmp_name = f".status.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp_name, flags, 0o600, dir_fd=self.directory_fd)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            _write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(
                tmp_name,
                "status",
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def read_commands(self, offset: int) -> tuple[bytes, int]:
        opened = os.fstat(self.commands_fd)
        current = self._current_commands_stat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise OSError("commands.ndjson was replaced during the session")
        if opened.st_size < offset:
            raise OSError("commands.ndjson must be append-only")
        if opened.st_size == offset:
            return b"", offset
        os.lseek(self.commands_fd, offset, os.SEEK_SET)
        remaining = opened.st_size - offset
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(self.commands_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        return payload, offset + len(payload)

    def _current_commands_stat(self) -> os.stat_result:
        if self.directory_fd is not None:
            return os.stat(
                "commands.ndjson",
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
        return os.lstat(self.directory / "commands.ndjson")


async def run_attach(
    bundle_path: Path,
    passcode: str,
    *,
    daemon_url: str = "http://127.0.0.1:63387",
    session_dir: Optional[Path] = None,
) -> int:
    prepared = _prepare_attach(bundle_path, daemon_url, session_dir)
    if prepared is None:
        return 2
    bundle_b64, session_dir, files, ws_url = prepared
    print(f"SESSION_DIR={session_dir}", flush=True)
    try:
        return await _run_attach_connection(bundle_b64, passcode, files, ws_url)
    finally:
        files.close()


def _prepare_attach(
    bundle_path: Path, bridge_url: str, session_dir: Path | None
) -> tuple[str, Path, _SessionFiles, str] | None:
    if not bundle_path.is_file():
        print(f"error: bundle not found: {bundle_path}", file=sys.stderr)
        return None
    if session_dir is None:
        session_dir = (
            Path(tempfile.gettempdir()) / f"puffo-attach-{secrets.token_hex(4)}"
        )
    try:
        if session_dir.is_symlink():
            raise OSError("session directory must not be a symlink")
        session_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(session_dir, 0o700)
    except OSError as exc:
        print(f"error: cannot prepare session directory: {exc}", file=sys.stderr)
        return None
    try:
        files = _open_session_files(session_dir)
    except OSError as exc:
        print(f"error: cannot initialize session files: {exc}", file=sys.stderr)
        return None
    url = (
        bridge_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        + "/v1/ws-local"
    )
    try:
        bundle = base64.b64encode(bundle_path.read_bytes()).decode("ascii")
    except OSError as exc:
        files.close()
        print(f"error: cannot read bundle: {exc}", file=sys.stderr)
        return None
    return bundle, session_dir, files, url


def _open_session_files(directory: Path) -> _SessionFiles:
    directory_fd: int | None = None
    events_fd: int | None = None
    commands_fd: int | None = None
    try:
        if os.name != "nt":
            before = os.lstat(directory)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            directory_fd = os.open(directory, flags)
            opened = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise OSError("session directory changed during setup")
        events_fd = _open_protocol_file(
            directory, directory_fd, "events.ndjson", os.O_WRONLY | os.O_APPEND
        )
        commands_fd = _open_protocol_file(
            directory, directory_fd, "commands.ndjson", os.O_RDWR
        )
        _unlink_status(directory, directory_fd)
        return _SessionFiles(directory, directory_fd, events_fd, commands_fd)
    except BaseException:
        for fd in (events_fd, commands_fd, directory_fd):
            if fd is not None:
                os.close(fd)
        raise


def _open_protocol_file(
    directory: Path,
    directory_fd: int | None,
    name: str,
    access_flags: int,
) -> int:
    flags = access_flags | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    path = directory / name
    if directory_fd is None and path.is_symlink():
        raise OSError(f"protocol file must not be a symlink: {name}")
    if directory_fd is not None:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    else:
        fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    raise OSError(f"protocol file must be regular: {name}")


def _unlink_status(directory: Path, directory_fd: int | None) -> None:
    try:
        if directory_fd is not None:
            os.unlink("status", dir_fd=directory_fd)
        else:
            (directory / "status").unlink()
    except FileNotFoundError:
        pass


async def _run_attach_connection(
    bundle: str, passcode: str, files: _SessionFiles, url: str
) -> int:
    emit, write_status = _attach_file_writers(files)
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
        ws_error: str | None = None
        try:
            ws_error, _ = await asyncio.gather(
                _pump_ws(ws, stop, emit, write_status),
                _pump_commands(ws, stop, files, emit),
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            if not ws.closed:
                await ws.close()
    return 1 if ws_error else 0


def _attach_file_writers(
    files: _SessionFiles,
) -> tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]]:
    return files.emit, files.write_state


async def _pump_ws(
    ws: aiohttp.ClientWebSocketResponse,
    stop: asyncio.Event,
    emit: Callable[[dict[str, Any]], None],
    write_status: Callable[[dict[str, Any]], None],
) -> str | None:
    connected = False
    terminal_error: str | None = None
    try:
        async for message in ws:
            if stop.is_set():
                break
            became_connected, error = await _handle_ws_message(
                ws, message, stop, emit, write_status
            )
            connected = connected or became_connected
            if error:
                terminal_error = error
                break
    finally:
        if terminal_error is None and not stop.is_set():
            terminal_error = (
                "connection closed unexpectedly"
                if connected
                else "connection closed before the handshake completed"
            )
        emit({"type": "disconnected"})
        if terminal_error:
            write_status({"state": "error", "reason": terminal_error})
        else:
            write_status({"state": "disconnected"})
        stop.set()
    return terminal_error


async def _handle_ws_message(
    ws: aiohttp.ClientWebSocketResponse,
    message: aiohttp.WSMessage,
    stop: asyncio.Event,
    emit: Callable[[dict[str, Any]], None],
    write_status: Callable[[dict[str, Any]], None],
) -> tuple[bool, str | None]:
    if message.type == aiohttp.WSMsgType.TEXT:
        try:
            frame = json.loads(message.data)
        except ValueError:
            reason = "non-JSON WS frame"
            emit({"type": "error", "reason": reason})
            stop.set()
            return False, reason
        emit(frame)
        kind = frame.get("type")
        if kind == "connected":
            write_status({"state": "connected", "agent": frame.get("agent", {})})
            return True, None
        elif kind == "error":
            reason = str(frame.get("reason", "") or "ws-local connection rejected")
            write_status({"state": "error", "reason": reason})
            stop.set()
            return False, reason
        elif kind == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
        return False, None
    elif message.type == aiohttp.WSMsgType.ERROR:
        reason = f"ws error: {ws.exception()}"
        emit({"type": "error", "reason": reason})
        stop.set()
        return False, reason
    elif message.type not in {
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.CLOSING,
    }:
        reason = f"unsupported WS frame type: {message.type.name}"
        emit({"type": "error", "reason": reason})
        stop.set()
        return False, reason
    return False, None


async def _pump_commands(
    ws: aiohttp.ClientWebSocketResponse,
    stop: asyncio.Event,
    files: _SessionFiles,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    offset = 0
    while not stop.is_set():
        try:
            chunk, offset = files.read_commands(offset)
        except OSError as exc:
            emit({"type": "error", "reason": str(exc)})
            stop.set()
            await ws.close()
            return
        if chunk:
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
