"""Daemon-owned machine control client (v0.4). Connects the machine control
WebSocket, receives operator-signed command envelopes, verifies + decrypts +
executes them, and acks. One WS per machine (commands for all linked operators
arrive on it, tagged with operator_slug)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from ...crypto.http_session import create_remote_http_session
from ..state import (
    AgentConfig,
    agent_yml_path,
    archive_flag_path,
    archived_dir,
    derive_role_short,
    discover_agents,
    refresh_agent_flag_path,
    refresh_host_sync_flag_path,
    refresh_model_flag_path,
    refresh_session_flag_path,
)
from . import machine_auth
from .envelope import TS_WINDOW_MS, ControlError, decrypt_command
from .store import load_or_create_machine, load_pairings, now_ms
from .usage_snapshot import collect_usage_snapshot

log = logging.getLogger("puffo_agent.control")

RECONNECT_BACKOFF_SECONDS = 3.0
ME_INTERVAL_SECONDS = 30.0
# Codex's probe costs a real (tiny) turn — slow cadence; refresh_usage is on-demand.
USAGE_INTERVAL_SECONDS = 6 * 60 * 60.0
RESCAN_SECONDS = 5.0
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Control-WS heartbeat: liveness ping + capability re-check cadence. Must stay
# well under the server's HEARTBEAT_TIMEOUT (90s) or the server culls us.
HEARTBEAT_INTERVAL_SECONDS = 30.0


def _is_already_archived(agent_slug: str) -> bool:
    # Matches any archived/<slug>-* suffix (-ws-/-del-/bare-stamp).
    root = archived_dir()
    if not root.exists():
        return False
    prefix = f"{agent_slug}-"
    return any(
        child.is_dir() and child.name.startswith(prefix)
        for child in root.iterdir()
    )


def _touch_flag(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _write_flag_payload(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _apply_refresh(agent_slug: str, params: dict) -> dict:
    """Control-ws mirror of the MCP ``refresh()`` tool; ``kind`` is
    rejected here (CLI + tray UI only)."""
    import time as _time

    if "kind" in params:
        return {
            "ok": False,
            "error": (
                "refresh over control-ws cannot change runtime kind; "
                "use puffo-agent CLI or the tray UI."
            ),
        }
    harness = params.get("harness")
    model = params.get("model")
    # PUF-431: the web's Refresh button sends no params, and a user who
    # hits Refresh wants everything current from disk — session dropped,
    # profile.md + memory reloaded, MCP + skills re-synced. Callers that
    # want a narrower refresh pass the flags explicitly.
    host_sync = bool(params.get("host_sync", True))
    session = bool(params.get("session", True))
    if (harness is None) != (model is None):
        return {
            "ok": False,
            "error": "harness and model must be provided together (or both omitted)",
        }

    try:
        cfg = AgentConfig.load(agent_slug)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    runtime_kind = cfg.runtime.kind
    if runtime_kind not in ("cli-local", "cli-docker"):
        return {
            "ok": False,
            "error": (
                f"refresh requires cli-local or cli-docker; agent is "
                f"kind={runtime_kind!r}"
            ),
        }
    if (
        host_sync
        and runtime_kind == "cli-docker"
        and not session
        and harness is None
    ):
        return {
            "ok": False,
            "error": (
                "refresh(host_sync=True) on cli-docker requires "
                "session=True or a harness+model swap"
            ),
        }

    workspace = cfg.resolve_workspace_dir()
    now = int(_time.time())
    touched: list[str] = []
    if harness is not None:
        _write_flag_payload(
            refresh_model_flag_path(workspace),
            {"harness": str(harness), "model": str(model), "requested_at": now},
        )
        touched.append("refresh_model")
    else:
        _write_flag_payload(
            refresh_agent_flag_path(workspace), {"requested_at": now},
        )
        touched.append("refresh_agent")
        if host_sync:
            _write_flag_payload(
                refresh_host_sync_flag_path(workspace), {"requested_at": now},
            )
            touched.append("refresh_host_sync")
        if session:
            _write_flag_payload(
                refresh_session_flag_path(workspace), {"requested_at": now},
            )
            touched.append("refresh_session")
    return {"ok": True, "touched": touched}


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

    from .provision import ProvisionError, provision_agent_from_bundle

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


async def post_usage_snapshot(machine, base: str) -> bool:
    """Collect the machine's usage-budget snapshot and POST it to the server.
    Returns True iff there was a snapshot to send. Shared by the periodic loop
    and the on-demand ``refresh_usage`` command."""
    snapshot = await collect_usage_snapshot(Path.home())
    if not snapshot:
        return False
    path = f"/v2/machines/{machine.machine_id}/usage"
    body = json.dumps({"snapshot": snapshot}).encode()
    headers = machine_auth.signed_headers(machine, "POST", path, body)
    headers["content-type"] = "application/json"
    async with create_remote_http_session(base) as session:
        resp = await session.post(f"{base}{path}", data=body, headers=headers)
        if resp.status >= 300:
            log.debug("control: usage report HTTP %s", resp.status)
    return True


async def execute_command(
    op: str,
    agent_slug: str | None,
    params: dict,
    *,
    server_url: str | None = None,
    paired_root_pubkey: str | None = None,
    command_id: str | None = None,
) -> dict:
    """Apply a decrypted command to local agent state for the reconciler.

    ``create`` additionally finalizes the pending identity with puffo-server
    and therefore needs the operator pairing context.
    """
    if op in {"runtime.cancel_turn", "runtime.resolve_permission"}:
        return await _execute_runtime_command(op, agent_slug, params, command_id)
    if op in ("pause", "resume", "edit", "archive", "refresh"):
        if not agent_slug or not agent_yml_path(agent_slug).exists():
            # Re-archive of an already-archived agent is idempotent OK.
            if op == "archive" and agent_slug and _is_already_archived(agent_slug):
                return {
                    "ok": True,
                    "note": "already archived",
                    "agent_slug": agent_slug,
                }
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
        return _apply_refresh(agent_slug, params)
    if op == "edit":
        return await _execute_edit(agent_slug, params)
    if op == "refresh_usage":
        # Machine-level (no agent_slug) — re-probe /usage and POST now instead
        # of waiting for the periodic tick.
        if not server_url:
            return {"ok": False, "error": "refresh_usage: no server_url"}
        posted = await post_usage_snapshot(
            load_or_create_machine(), server_url.rstrip("/")
        )
        return {"ok": True, "posted": posted}
    if op == "create":
        return await _create_agent_command(params, server_url, paired_root_pubkey)
    # export/import carry bigger flows; not yet wired.
    return {"ok": False, "error": f"unsupported op {op!r}"}


async def _execute_runtime_command(
    op: str,
    slug: str | None,
    params: dict,
    command_id: str | None,
) -> dict:
    from ...agent.harness.runtime_commands import execute_runtime_command

    command = dict(params)
    command.update(
        command_id=str(command_id or ""),
        agent_id=str(slug or ""),
        op=op.removeprefix("runtime."),
    )
    return await execute_runtime_command(
        command,
        expected_agent_id=str(slug or ""),
        manager_agent_id=str(slug or ""),
        require_version=False,
        permission_turn_from_active=False,
    )


def _set_agent_state(agent_slug: str | None, state: str) -> dict:
    cfg = AgentConfig.load(agent_slug)
    cfg.state = state
    cfg.save()
    return {"ok": True, "state": state}


async def _execute_edit(agent_slug: str | None, params: dict) -> dict:
    cfg = AgentConfig.load(agent_slug)
    patch, prompt_changed = _apply_edit_profile_fields(cfg, params)
    runtime_changed, error = _apply_edit_runtime(cfg, params)
    if error is not None:
        return {"ok": False, "error": error}
    env_changed, error = _apply_edit_env_overrides(cfg, params)
    if error is not None:
        return {"ok": False, "error": error}
    runtime_changed = runtime_changed or env_changed

    cfg.save()
    prompt_changed = _write_edit_profile(agent_slug, cfg, params) or prompt_changed
    if isinstance(params.get("role"), str):
        from ..profile_sync import update_profile_role

        update_profile_role(cfg, params["role"])
    await _sync_edit_profile(cfg, patch)
    if cfg.state == "running" and prompt_changed and not runtime_changed:
        _write_flag_payload(
            refresh_agent_flag_path(cfg.resolve_workspace_dir()),
            {"requested_at": int(__import__("time").time())},
        )
    return {"ok": True}


def _apply_edit_profile_fields(cfg: AgentConfig, params: dict) -> tuple[dict, bool]:
    patch: dict = {}
    prompt_changed = False
    for field in ("display_name", "role"):
        value = params.get(field)
        if isinstance(value, str):
            setattr(cfg, field, value)
            patch[field] = value
            prompt_changed = True
    if isinstance(params.get("role"), str):
        cfg.role_short = derive_role_short(params["role"])
        patch["role_short"] = cfg.role_short
    if isinstance(params.get("avatar_url"), str):
        cfg.avatar_url = params["avatar_url"]
        patch["avatar_url"] = params["avatar_url"]
    if isinstance(params.get("soul"), str):
        patch["soul"] = params["soul"]
        prompt_changed = True
    return patch, prompt_changed


def _apply_edit_runtime(cfg: AgentConfig, params: dict) -> tuple[bool, str | None]:
    raw = params.get("runtime")
    if not isinstance(raw, dict):
        return False, None

    rt = cfg.runtime
    before = (rt.kind, rt.provider, rt.harness, rt.model, rt.inference_level)
    for key in ("kind", "provider", "harness", "model"):
        value = raw.get(key)
        if isinstance(value, str):
            setattr(rt, key, value)

    from ..runtime_matrix import (
        normalize_inference_level,
        resolve_effective_harness,
        resolve_effective_provider,
        validate_triple,
    )

    result = validate_triple(rt.kind, rt.provider, rt.harness)
    if not result.ok:
        return False, f"runtime: {result.error}"

    incoming_level = raw.get("inference_level")
    if isinstance(incoming_level, str):
        if incoming_level:
            from ...mcp.config import supported_inference_levels

            effective_harness = resolve_effective_harness(
                rt.kind,
                resolve_effective_provider(rt.kind, rt.provider),
                rt.harness,
            )
            supported = supported_inference_levels(effective_harness)
            if incoming_level not in supported:
                return False, (
                    "runtime.inference_level must be one of: "
                    + ", ".join(supported)
                )
        rt.inference_level = incoming_level
    else:
        rt.inference_level = normalize_inference_level(
            rt.kind,
            rt.provider,
            rt.harness,
            rt.inference_level,
        )

    after = (rt.kind, rt.provider, rt.harness, rt.model, rt.inference_level)
    return before != after, None


def _apply_edit_env_overrides(
    cfg: AgentConfig, params: dict
) -> tuple[bool, str | None]:
    if "env_overrides" not in params:
        return False, None

    from ..state import merge_env_overrides
    from .context_telemetry import (
        CLAUDE_COMPACT_PCT_KEY,
        CODEX_COMPACT_PCT_KEY,
    )

    updates = params.get("env_overrides")
    if (
        cfg.runtime.harness == "codex"
        and isinstance(updates, dict)
        and CLAUDE_COMPACT_PCT_KEY in updates
        and CODEX_COMPACT_PCT_KEY not in updates
    ):
        updates = dict(updates)
        updates[CODEX_COMPACT_PCT_KEY] = updates.pop(CLAUDE_COMPACT_PCT_KEY)
        if CLAUDE_COMPACT_PCT_KEY in cfg.env_overrides:
            updates[CLAUDE_COMPACT_PCT_KEY] = ""
    try:
        merged = merge_env_overrides(cfg.env_overrides, updates)
    except ValueError as exc:
        return False, str(exc)
    if merged == cfg.env_overrides:
        return False, None
    cfg.env_overrides = merged
    return True, None


def _write_edit_profile(
    agent_slug: str | None, cfg: AgentConfig, params: dict
) -> bool:
    if not isinstance(params.get("profile"), str):
        return False
    (agent_yml_path(agent_slug).parent / cfg.profile).write_text(
        params["profile"], encoding="utf-8"
    )
    return True


async def _sync_edit_profile(cfg: AgentConfig, patch: dict) -> None:
    if not patch:
        return
    try:
        from ..profile_sync import sync_agent_profile

        await sync_agent_profile(cfg, patch)
    except Exception as exc:  # noqa: BLE001
        log.warning("control: edit profile sync failed: %s", exc)


def _ws_url(base: str) -> str:
    # http→ws / https→wss
    return base.replace("http", "ws", 1) + "/v2/machines/subscribe"


async def _sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def build_capabilities() -> dict:
    """Return CLI authentication and model capabilities for the portal."""
    from ...agent.cli_bin import (
        claude_has_credentials,
        cli_tool_status,
        codex_has_credentials,
        resolve_claude_bin,
        resolve_codex_bin,
    )
    from ...agent.model_catalog import KNOWN_HARNESSES, provider_models

    import importlib.metadata

    try:
        daemon_version = importlib.metadata.version("puffo-agent")
    except Exception:  # noqa: BLE001
        daemon_version = ""

    cli_tools = {
        "claude-code": cli_tool_status(resolve_claude_bin, claude_has_credentials),
        "codex": cli_tool_status(resolve_codex_bin, codex_has_credentials),
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
        async with create_remote_http_session(base) as session:
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
                command_id=str(command_id or ""),
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
        usage_task: asyncio.Task | None = None
        try:
            while not self._stop.is_set():
                pairings = load_pairings()
                if pairings and machine is None:
                    machine = load_or_create_machine()
                if pairings and ws_task is None:
                    client = MachineControlClient(machine)
                    ws_task = asyncio.create_task(client.run(self._stop))
                    me_task = asyncio.create_task(self._me_loop(machine))
                    usage_task = asyncio.create_task(self._usage_loop(machine))
                if not pairings and ws_task is not None:
                    ws_task.cancel()
                    me_task.cancel()
                    usage_task.cancel()
                    ws_task = me_task = usage_task = None
                await _sleep_or_stop(self._stop, RESCAN_SECONDS)
        finally:
            for t in (ws_task, me_task, usage_task):
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
                    async with create_remote_http_session(base) as session:
                        await session.post(f"{base}/v2/machines/me", data=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 — best-effort liveness
                log.debug("control: /me report failed: %s", exc)
            await _sleep_or_stop(self._stop, ME_INTERVAL_SECONDS)

    async def _usage_loop(self, machine) -> None:
        """Probe each runtime's /usage budget and POST the machine's snapshot.
        Replace-latest server-side, so a dropped tick just resends next time."""
        while not self._stop.is_set():
            try:
                pairings = load_pairings()
                if pairings:
                    base = next(iter(pairings.values())).server_url.rstrip("/")
                    await post_usage_snapshot(machine, base)
            except Exception as exc:  # noqa: BLE001 — best-effort; retry next tick
                log.debug("control: usage report failed: %s", exc)
            await _sleep_or_stop(self._stop, USAGE_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
