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
from .envelope import TS_WINDOW_MS, ControlError, decrypt_command
from .store import load_or_create_machine, load_pairings, now_ms

log = logging.getLogger("puffo_agent.control")

RECONNECT_BACKOFF_SECONDS = 3.0
ME_INTERVAL_SECONDS = 30.0
RESCAN_SECONDS = 5.0
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Control-WS heartbeat: liveness ping + capability re-check cadence. Must stay
# well under the server's HEARTBEAT_TIMEOUT (90s) or the server culls us.
HEARTBEAT_INTERVAL_SECONDS = 30.0


def _touch_flag(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


async def _materialize_slug_binding(
    server_url: str, pending_token: str, slug_binding: dict
) -> None:
    """Finalize a pending agent identity on puffo-server by POSTing the
    browser-pre-built slug_binding (agent self-signed, transport-
    unauthenticated). Done on the machine *after* delivery, so a command that
    never arrives leaves only a TTL'd pending row — never a permanent orphan."""
    from ...crypto.http_session import create_remote_http_session

    body = {"pending_token": pending_token, "slug_binding": slug_binding}
    async with create_remote_http_session(server_url, timeout=HTTP_TIMEOUT) as session:
        async with session.post(
            f"{server_url.rstrip('/')}/certs/slug_binding", json=body
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise ControlError(f"slug_binding {resp.status}: {text}")


async def _create_agent_command(
    params: dict, server_url: str | None, paired_root_pubkey: str | None
) -> dict:
    """Agent Portal remote create: materialize the pending identity, then write
    the agent dir. Order is verify → materialize → write, so a verify failure
    never materializes and a never-delivered command never registers."""
    if not server_url or not paired_root_pubkey:
        return {"ok": False, "error": "create missing operator pairing context"}
    pending_token = params.get("pending_token")
    slug_binding = (params.get("identity_bundle") or {}).get("slug_binding")
    if not isinstance(pending_token, str) or not pending_token:
        return {"ok": False, "error": "create missing pending_token"}
    if not isinstance(slug_binding, dict):
        return {"ok": False, "error": "create missing identity_bundle.slug_binding"}

    # Browser server_url placeholders are unreachable here; stamp our paired
    # server_url so the worker talks to a URL the machine can reach.
    pc = params.get("puffo_core")
    if isinstance(pc, dict):
        pc["server_url"] = server_url

    from ..api.handlers import ProvisionError, provision_agent_from_bundle

    async def _materialize(_ctx: dict) -> None:
        await _materialize_slug_binding(server_url, pending_token, slug_binding)

    try:
        result = await provision_agent_from_bundle(
            params, paired_root_pubkey, materialize=_materialize
        )
    except ProvisionError as exc:
        return {"ok": False, "error": exc.reason}
    except ControlError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "agent_slug": result["agent_id"]}


async def execute_command(
    op: str,
    agent_slug: str | None,
    params: dict,
    *,
    server_url: str | None = None,
    paired_root_pubkey: str | None = None,
) -> dict:
    """Apply a decrypted command to local agent state, the same way the local
    bridge handlers do (flip ``agent.yml`` state, drop sentinel flags) so the
    reconcile loop applies it — a single-writer model. ``create`` additionally
    finalizes the pending identity with puffo-server (needs the operator
    pairing context)."""
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
        patch: dict = {}
        prompt_changed = False
        runtime_changed = False
        if isinstance(params.get("display_name"), str):
            cfg.display_name = params["display_name"]
            patch["display_name"] = params["display_name"]
            prompt_changed = True
        if isinstance(params.get("role"), str):
            cfg.role = params["role"]
            patch["role"] = params["role"]
            prompt_changed = True
        # avatar_url points to a blob the operator already uploaded; sync it to
        # the server identity (avatars are public, so no gating needed).
        if isinstance(params.get("avatar_url"), str):
            cfg.avatar_url = params["avatar_url"]
            patch["avatar_url"] = params["avatar_url"]
        # Soul is owner-gated text on the server identity (not kept in
        # agent.yml); the profile.md body carries it for the worker.
        if isinstance(params.get("soul"), str):
            patch["soul"] = params["soul"]
            prompt_changed = True
        # Runtime block (kind/provider/harness/model) — same fields the local
        # bridge's update_runtime accepts; reject invalid triples before saving.
        rt_in = params.get("runtime")
        if isinstance(rt_in, dict):
            rt = cfg.runtime
            for key in ("kind", "provider", "harness", "model"):
                if isinstance(rt_in.get(key), str):
                    setattr(rt, key, rt_in[key])
                    runtime_changed = True
            from ..runtime_matrix import validate_triple

            result = validate_triple(rt.kind, rt.provider, rt.harness)
            if not result.ok:
                return {"ok": False, "error": f"runtime: {result.error}"}
        cfg.save()
        if isinstance(params.get("profile"), str):
            (agent_yml_path(agent_slug).parent / cfg.profile).write_text(
                params["profile"], encoding="utf-8"
            )
            prompt_changed = True
        if patch:
            try:
                from ..api.handlers import _sync_agent_profile

                await _sync_agent_profile(cfg, patch)
            except Exception as exc:  # noqa: BLE001
                log.warning("control: edit profile sync failed: %s", exc)
        # restart.flag for runtime (spawn-args change); reload.flag
        # for prompt-only (lazy, preserves the AI session).
        if cfg.state == "running":
            if runtime_changed:
                _touch_flag(restart_flag_path(agent_slug))
            elif prompt_changed:
                from ..profile_sync import write_reload_flag
                write_reload_flag(cfg, reason="control-ws edit")
        return {"ok": True}
    if op == "create":
        return await _create_agent_command(params, server_url, paired_root_pubkey)
    if op == "agent_create_approved":
        # The operator approved a machine-initiated ws-local create — hand the
        # server-minted slug + pending_token to the waiting create flow.
        from .agent_create import get_pending_approvals

        resolved = get_pending_approvals().resolve(
            str(params.get("request_id") or ""),
            {"agent_slug": params.get("agent_slug"), "pending_token": params.get("pending_token")},
        )
        return {"ok": resolved, "error": None if resolved else "no pending create request"}
    # export/import carry bigger flows; not yet wired.
    return {"ok": False, "error": f"unsupported op {op!r}"}


def _ws_url(base: str) -> str:
    # http→ws / https→wss
    return base.replace("http", "ws", 1) + "/v2/machines/subscribe"


async def _sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def build_capabilities() -> dict:
    """This machine's reportable capabilities: CLI-tool auth status + provider/
    model catalog. Mirrors the local bridge's ``info.cli_tools`` + ``/v1/
    providers`` so the portal renders a remote machine's providers like a local
    one. ``fetch=False`` keeps it off the network (serves cache/static)."""
    from ...agent.cli_bin import (
        claude_has_credentials,
        codex_has_credentials,
        resolve_claude_bin,
        resolve_codex_bin,
    )
    from ...agent.model_catalog import KNOWN_HARNESSES, provider_models
    from ..api.handlers import _cli_tool_status

    import importlib.metadata

    try:
        daemon_version = importlib.metadata.version("puffo-agent")
    except Exception:  # noqa: BLE001
        daemon_version = ""

    cli_tools = {
        "claude-code": _cli_tool_status(resolve_claude_bin, claude_has_credentials),
        "codex": _cli_tool_status(resolve_codex_bin, codex_has_credentials),
    }
    providers = [
        {
            "provider": h,
            "models": [
                {"id": o.id, "label": o.label, "alias": o.is_alias}
                for o in provider_models(h, fetch=False)
                if o.id
            ],
        }
        for h in KNOWN_HARNESSES
    ]
    return {"cli_tools": cli_tools, "providers": providers, "daemon_version": daemon_version}


class MachineControlClient:
    """Holds the single control WS; verifies each command against the pinned
    operator root named in the frame, executes it, and acks."""

    def __init__(self, machine) -> None:
        self.machine = machine
        self._seen_nonces: dict[str, int] = {}  # nonce -> ts; pruned to the ts window
        # Serialize WS writes — acks (receive loop) + heartbeat/capabilities
        # (sender task) share one socket; concurrent send_json would interleave.
        self._send_lock = asyncio.Lock()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._connect_once(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect through transient errors
                log.debug("control: ws error: %s", exc)
            await _sleep_or_stop(stop, RECONNECT_BACKOFF_SECONDS)

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, obj: dict) -> None:
        async with self._send_lock:
            await ws.send_json(obj)

    async def _connect_once(self, stop: asyncio.Event) -> None:
        pairings = load_pairings()
        if not pairings:
            return
        base = next(iter(pairings.values())).server_url.rstrip("/")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_ws_url(base), heartbeat=None) as ws:
                await self._send(ws, machine_auth.ws_connect_frame(self.machine))
                # Initial capability snapshot on connect.
                last_caps = await asyncio.to_thread(build_capabilities)
                await self._send(ws, {"type": "capabilities", "capabilities": last_caps})
                sender = asyncio.create_task(self._heartbeat_loop(ws, stop, last_caps))

                # Register the reverse-channel sender on this live socket.
                from .reporter import get_reporter

                async def _report(operator_slug: str, envelope: dict) -> None:
                    await self._send(
                        ws,
                        {"type": "message", "operator_slug": operator_slug, "envelope": envelope},
                    )

                get_reporter().set_sender(_report)
                log.info("control: WS connected; agent.status sender ready")
                try:
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
                finally:
                    get_reporter().set_sender(None)
                    sender.cancel()
                    try:
                        await sender
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    async def _heartbeat_loop(
        self, ws: aiohttp.ClientWebSocketResponse, stop: asyncio.Event, initial_caps: dict
    ) -> None:
        """Periodic liveness ping; re-push capabilities only when they change
        (e.g. a CLI tool gets authed). Capability compute runs off-thread so a
        stale model-catalog fetch never blocks the heartbeat."""
        last_caps = initial_caps
        while not stop.is_set():
            await _sleep_or_stop(stop, HEARTBEAT_INTERVAL_SECONDS)
            if stop.is_set():
                break
            await self._send(ws, {"type": "heartbeat"})
            caps = await asyncio.to_thread(build_capabilities)
            if caps != last_caps:
                await self._send(ws, {"type": "capabilities", "capabilities": caps})
                last_caps = caps

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
                # Bound the replay set: a nonce older than the ts window is
                # already rejected by decrypt_command, so it's safe to forget.
                cutoff = now_ms() - TS_WINDOW_MS
                self._seen_nonces = {
                    n: t for n, t in self._seen_nonces.items() if t > cutoff
                }
                self._seen_nonces[nonce] = int(envelope.get("ts", now_ms()))
            result = await execute_command(
                decrypted["op"],
                decrypted["agent_slug"],
                decrypted["params"],
                server_url=pairing.server_url,
                paired_root_pubkey=pairing.operator_root_pubkey,
            )
            if isinstance(result, dict) and not result.get("ok", True):
                log.warning(
                    "control: command %s op=%s failed: %s",
                    command_id, decrypted["op"], result.get("error"),
                )
        except ControlError as exc:
            # Forged / malformed → never execute, but ack so it stops redelivering.
            log.warning("control: rejected command %s: %s", command_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("control: command %s failed: %s", command_id, exc)

        if command_id:
            try:
                await self._send(ws, {"type": "ack", "command_id": command_id})
            except Exception as exc:  # noqa: BLE001
                log.debug("control: ack %s failed: %s", command_id, exc)


class ControlManager:
    """Starts the machine control WS + a periodic self-report once the machine
    has at least one operator pairing. Re-scans so link/unlink take effect
    without a daemon restart.

    Constraint: one control WS per machine, bound to the first pairing's
    ``server_url``. Multiple operators on that same server are served; a
    machine paired across two different servers only serves the first."""

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
