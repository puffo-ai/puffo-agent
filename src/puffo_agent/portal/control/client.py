"""Daemon-owned machine control client (v0.4). Connects the machine control
WebSocket, receives operator-signed command envelopes, verifies + decrypts +
executes them, and acks. One WS per machine (commands for all linked operators
arrive on it, tagged with operator_slug)."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from ..state import (
    AgentConfig,
    agent_yml_path,
    archive_flag_path,
    discover_agents,
    restart_flag_path,
)
from . import machine_auth
from .envelope import ControlError, decrypt_command
from .store import load_or_create_machine, load_pairings, now_ms

log = logging.getLogger("puffo_agent.control")

RECONNECT_BACKOFF_SECONDS = 3.0
ME_INTERVAL_SECONDS = 30.0
RESCAN_SECONDS = 5.0


def _touch_flag(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def execute_command(op: str, agent_slug: str | None, params: dict) -> dict:
    """Apply a decrypted command to local agent state, the same way the local
    bridge handlers do (flip ``agent.yml`` state, drop sentinel flags) so the
    reconcile loop applies it — a single-writer model."""
    if op in ("pause", "resume", "edit", "archive", "refresh"):
        if not agent_slug or not agent_yml_path(agent_slug).exists():
            return {"ok": False, "error": f"unknown agent {agent_slug!r}"}

    if op == "pause":
        cfg = AgentConfig.load(agent_slug)
        cfg.state = "paused"
        cfg.save()
        return {"ok": True, "state": "paused"}
    if op == "resume":
        cfg = AgentConfig.load(agent_slug)
        cfg.state = "running"
        cfg.save()
        return {"ok": True, "state": "running"}
    if op == "archive":
        _touch_flag(archive_flag_path(agent_slug))
        return {"ok": True}
    if op == "refresh":
        _touch_flag(restart_flag_path(agent_slug))
        return {"ok": True}
    if op == "edit":
        cfg = AgentConfig.load(agent_slug)
        if isinstance(params.get("display_name"), str):
            cfg.display_name = params["display_name"]
        if isinstance(params.get("role"), str):
            cfg.role = params["role"]
        cfg.save()
        if isinstance(params.get("profile"), str):
            (agent_yml_path(agent_slug).parent / cfg.profile).write_text(
                params["profile"], encoding="utf-8"
            )
        return {"ok": True}
    # create/export/import carry key material + bigger flows; not yet wired.
    return {"ok": False, "error": f"unsupported op {op!r}"}


def _ws_url(base: str) -> str:
    # http→ws / https→wss
    return base.replace("http", "ws", 1) + "/v2/machines/subscribe"


async def _sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


class MachineControlClient:
    """Holds the single control WS; verifies each command against the pinned
    operator root named in the frame, executes it, and acks."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self._seen_nonces: set[str] = set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._connect_once(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect through transient errors
                log.debug("control: ws error: %s", exc)
            await _sleep_or_stop(stop, RECONNECT_BACKOFF_SECONDS)

    async def _connect_once(self, stop: asyncio.Event) -> None:
        pairings = load_pairings()
        if not pairings:
            return
        base = next(iter(pairings.values())).server_url.rstrip("/")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_ws_url(base), heartbeat=None) as ws:
                await ws.send_json(machine_auth.ws_connect_frame(self.machine))
                async for msg in ws:
                    if stop.is_set():
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        continue
                    if frame.get("type") == "command":
                        await self._handle(ws, frame)
                    elif frame.get("type") == "error":
                        log.warning("control: server rejected ws: %s", frame.get("reason"))
                        break

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, frame: dict) -> None:
        command_id = frame.get("command_id")
        operator_slug = frame.get("operator_slug")
        try:
            pairing = load_pairings().get(operator_slug)
            if pairing is None:
                raise ControlError(f"no pairing for operator {operator_slug!r}")
            envelope = frame.get("envelope") or {}
            nonce = envelope.get("nonce")
            if nonce and nonce in self._seen_nonces:
                raise ControlError("replayed nonce")
            decrypted = decrypt_command(
                envelope, self.machine, pairing.operator_root_pubkey, now_ms()
            )
            if nonce:
                self._seen_nonces.add(nonce)
            execute_command(decrypted["op"], decrypted["agent_slug"], decrypted["params"])
        except ControlError as exc:
            # Forged / malformed → never execute, but ack so it stops redelivering.
            log.warning("control: rejected command %s: %s", command_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("control: command %s failed: %s", command_id, exc)

        if command_id:
            try:
                await ws.send_json({"type": "ack", "command_id": command_id})
            except Exception as exc:  # noqa: BLE001
                log.debug("control: ack %s failed: %s", command_id, exc)


class ControlManager:
    """Starts the machine control WS + a periodic self-report once the machine
    has at least one operator pairing. Re-scans so link/unlink take effect
    without a daemon restart."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()

    async def run(self) -> None:
        machine = None
        ws_task: asyncio.Task | None = None
        me_task: asyncio.Task | None = None
        try:
            while not self._stop.is_set():
                pairings = load_pairings()
                if pairings and machine is None:
                    machine = load_or_create_machine()
                if pairings and ws_task is None:
                    client = MachineControlClient(machine)
                    ws_task = asyncio.create_task(client.run(self._stop))
                    me_task = asyncio.create_task(self._me_loop(machine))
                if not pairings and ws_task is not None:
                    ws_task.cancel()
                    me_task.cancel()
                    ws_task = me_task = None
                await _sleep_or_stop(self._stop, RESCAN_SECONDS)
        finally:
            for t in (ws_task, me_task):
                if t is not None:
                    t.cancel()

    async def _me_loop(self, machine) -> None:
        while not self._stop.is_set():
            try:
                pairings = load_pairings()
                if pairings:
                    base = next(iter(pairings.values())).server_url.rstrip("/")
                    body = json.dumps({"agents": len(discover_agents())}).encode()
                    headers = machine_auth.signed_headers(machine, "POST", "/v2/machines/me", body)
                    headers["content-type"] = "application/json"
                    async with aiohttp.ClientSession() as session:
                        await session.post(f"{base}/v2/machines/me", data=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 — best-effort liveness
                log.debug("control: /me report failed: %s", exc)
            await _sleep_or_stop(self._stop, ME_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
