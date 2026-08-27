"""aiohttp WS route for ws-local tools: ``GET /v1/ws-local``.

Loopback-only. Authentication decrypts the ``.puffoagent`` bundle and binds
its root identity to the daemon's locally managed attach point. The handler
wires that point into ``serve_connection``: the session relays replies and
judges liveness, while the consumer feeds batches and advances the cursor.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from functools import partial
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from aiohttp import web

from .aiohttp_transport import AiohttpTransport
from .auth import authenticate_bundle
from .bundles import BundleQueue
from .endpoint import serve_connection
from .hub import AttachPoint, WsLocalHub
from .in_process_data_client import InProcessDataClient
from .protocol import Error, encode
from .session import Transport, WsLocalSession
from .tool_dispatch import build_dispatch as _build_dispatch
from ..state import shared_fs_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WsLocalContinuation:
    """Private correlation record; it never crosses the WS protocol."""

    callback: object
    planning_cycle_key: str
    correlation_receipt: str
    tool_names: frozenset[str]
    normalized_arguments: tuple[tuple[str, str], ...]


class _WsLocalContextAdapter:
    """Provider context/admission seam for one authenticated attachment."""

    def __init__(self) -> None:
        self._callback = None
        self._planning_cycle_key = ""
        self._continuations: dict[str, _WsLocalContinuation] = {}
        self._observed_tool_results: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self._provider_session_id: str | None = None

    async def get_context_snapshot(self):
        from ...agent.context_controller import ContextSnapshot

        return ContextSnapshot(0, 200_000, "ws-local", datetime.now(timezone.utc))

    def get_context_capabilities(self):
        from ...agent.context_controller import ContextCapabilities

        return ContextCapabilities()

    async def compact_context(self):
        raise RuntimeError("ws-local context compaction is unavailable")

    async def rollover_context(self):
        raise RuntimeError("ws-local context rollover is unavailable")

    def get_provider_session_id(self) -> str | None:
        return self._provider_session_id

    def register_admission_callback(self, callback, planning_cycle_key="") -> None:
        self._callback = callback
        self._planning_cycle_key = planning_cycle_key

    def register_autonomous_callback(self, callback) -> bool:
        """WS-local attachments do not expose autonomous provider turns."""
        del callback
        return False

    def register_continuation_callback(
        self,
        callback,
        planning_cycle_key="",
        *,
        correlation_receipt="",
        tool_names=(),
        tool_arguments: Mapping[str, Any] | None = None,
        **_metadata,
    ) -> None:
        from ...agent.context_controller import normalize_tool_arguments

        if callback is None:
            self._continuations.pop(planning_cycle_key, None)
            return
        entry = _WsLocalContinuation(
            callback=callback,
            planning_cycle_key=planning_cycle_key,
            correlation_receipt=correlation_receipt,
            tool_names=frozenset(str(name) for name in tool_names if name),
            normalized_arguments=normalize_tool_arguments(tool_arguments),
        )
        self._continuations[planning_cycle_key] = entry
        if correlation_receipt:
            self._continuations[correlation_receipt] = entry

    def observe_tool_result(
        self,
        correlation_receipt: str,
        tool_name: str,
        tool_arguments: Mapping[str, Any],
    ) -> None:
        """Record the private, actual WS tool invocation for its receipt."""
        if correlation_receipt:
            self._observed_tool_results[correlation_receipt] = (
                tool_name,
                dict(tool_arguments),
            )

    async def emit_admission(
        self,
        *,
        turn_id: str,
        correlation_key: str,
        tool_name: str | None = None,
        tool_arguments: Mapping[str, Any] | None = None,
    ) -> None:
        from ...agent.context_controller import ProviderAdmissionEvent
        from ...agent.context_controller import normalize_tool_arguments

        if self._callback is not None and correlation_key == self._planning_cycle_key:
            callback, self._callback = self._callback, None
            planning_cycle_key = correlation_key
        else:
            entry = self._continuations.get(correlation_key)
            if entry is None:
                raise RuntimeError("ws-local admission correlation failed")
            # The public frame intentionally carries only the opaque receipt.
            # The bridge supplies the actual private tool invocation recorded
            # when that receipt was returned; direct in-process callers may
            # pass the same evidence explicitly.
            if tool_name is None or tool_arguments is None:
                observed = self._observed_tool_results.get(correlation_key)
                if observed is None:
                    raise RuntimeError("ws-local admission tool result unavailable")
                tool_name, tool_arguments = observed
            if entry.tool_names and tool_name not in entry.tool_names:
                raise RuntimeError("ws-local admission tool correlation failed")
            if (
                entry.normalized_arguments
                and normalize_tool_arguments(tool_arguments)
                != entry.normalized_arguments
            ):
                raise RuntimeError("ws-local admission argument correlation failed")
            callback = entry.callback
            planning_cycle_key = entry.planning_cycle_key
            for key, registered in tuple(self._continuations.items()):
                if registered == entry:
                    self._continuations.pop(key, None)
            self._observed_tool_results.pop(correlation_key, None)
        self._provider_session_id = f"ws-local:{turn_id}"
        await callback(
            ProviderAdmissionEvent(
                planning_cycle_key=planning_cycle_key,
                provider_session_id=self._provider_session_id,
                provider_turn_id=turn_id,
                admitted_at=datetime.now(timezone.utc),
            )
        )


def _build_tool_dispatch(point: AttachPoint, runtime=None):
    from ...mcp.puffo_core_tools import PuffoCoreToolsConfig

    client = point.client
    # The runtime this session actually runs on is passed in explicitly: a
    # connection-owned runtime is no longer published on the client at
    # construction time (see ``_install_owned_runtime``). For the reused
    # worker runtime these are the same objects the client carries.
    send_coordinator = (
        getattr(runtime, "send_delegate", None)
        or getattr(client, "send_delegate", None)
    )
    inbox_runtime = runtime if runtime is not None else getattr(
        client, "global_runtime", None
    )
    cfg = PuffoCoreToolsConfig(
        slug=client.slug,
        agent_id=point.agent_id,
        device_id=client.device_id,
        keystore=client.keystore,
        http_client=client.http,
        data_client=InProcessDataClient(client.store, client),
        space_id=getattr(client, "space_id", None),
        workspace=getattr(client, "workspace", None),
        shared_workspace=str(shared_fs_dir()),
        message_client=client,
        send_coordinator=send_coordinator,
        inbox_runtime=inbox_runtime,
        # T23: the daemon owns the single per-agent bridge WS, so only
        # the in-process ws-local tools can drive it. None on native
        # agents → send_message keeps the signed-crypto path. The
        # subprocess/RPC MCP site can't own this WS, so it stays None.
        bridge_client=getattr(client, "_bridge", None),
    )
    return _build_dispatch(cfg)


WS_LOCAL_PATH = "/v1/ws-local"
WS_LOCAL_HUB_KEY = web.AppKey("ws_local_hub", WsLocalHub)


async def handle_ws_local(request: web.Request) -> web.WebSocketResponse:
    hub = request.app.get(WS_LOCAL_HUB_KEY)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    transport = AiohttpTransport(ws)
    if hub is None:
        await transport.send(encode(Error("ws-local is not enabled on this daemon")))
        await transport.close()
        return ws
    await serve_attached(transport, hub)
    return ws


async def serve_attached(transport: Transport, hub: WsLocalHub) -> None:
    """Wire the hub into ``serve_connection``. Split out from the aiohttp
    boilerplate so it's exercisable over any transport."""

    # Scoped to this connection: it carries the connection-owned runtime from
    # construction to attach without publishing it on the long-lived client,
    # so a connection that never attaches owns nothing to unwind.
    connection: dict[str, Any] = {}
    await serve_connection(
        transport,
        authenticate=authenticate_bundle,
        is_servable=hub.is_servable,
        agent_context=partial(_ws_agent_context, hub),
        registry=hub.registry,
        make_session=partial(_make_ws_session, hub, connection),
        start_consumer=partial(_start_ws_consumer, hub, connection),
        new_session_id=lambda: f"wsl_{uuid.uuid4().hex}",
        base64_decode=base64.b64decode,
    )


async def _ws_agent_context(hub: WsLocalHub, slug: str) -> dict:
    point = hub.get(slug)
    if point is None:
        return {}
    cfg = point.agent_cfg
    try:
        profile_md = Path(cfg.resolve_profile_path()).read_text(encoding="utf-8")
    except OSError:
        profile_md = ""
    return {
        "slug": slug,
        "display_name": getattr(cfg, "display_name", ""),
        "profile_md": profile_md,
    }


def _make_ws_session(
    hub: WsLocalHub, connection: dict, authed, session_id, transport, bridge,
    capabilities,
) -> WsLocalSession:
    point, client = hub.get(authed.slug), hub.get(authed.slug).client
    # No ``await`` between this read and ``registry.acquire`` in the endpoint,
    # so the decision and the claim are atomic on the event loop: a reused
    # runtime here can only be the worker's, never a detaching connection's.
    runtime = getattr(client, "global_runtime", None)
    holder: dict[str, WsLocalSession] = {}
    if runtime is None:
        # Capability-independent: the owned runtime is what supplies the
        # SendCoordinator/send delegate every send path needs, and it is the
        # only thing that schedules turns at all. Gating it on the v2
        # capabilities left a v1 peer both mute and unable to reply. The
        # capability check belongs at the wire-shape seam
        # (``WsLocalBridge.dispatch_planned``), not at construction.
        runtime = _make_owned_runtime(point, bridge, holder)
        # F1/B3: publishing it on the client is deferred to the attach scope
        # that unwinds it. Until then it belongs to this connection alone.
        connection["owned_runtime"] = runtime
    bridge._runtime = runtime
    session = WsLocalSession(
        slug=authed.slug,
        session_id=session_id,
        transport=transport,
        queue=BundleQueue(),
        reporter=point.reporter,
        tool_dispatch=_build_tool_dispatch(point, runtime),
        on_acked=bridge.on_acked,
        on_admitted=bridge.on_admitted,
        on_tool_result=bridge.on_tool_result,
        on_dead=bridge.on_dead,
        capabilities=capabilities,
        now=time.monotonic,
        ack_timeout_s=point.ack_timeout_s,
        ping_interval_s=point.ping_interval_s,
    )
    holder["session"] = session
    return session


def _make_owned_runtime(point: AttachPoint, bridge, holder: dict[str, WsLocalSession]):
    from ...agent.channel_audience import read_channel_audience
    from ...agent.global_inbox_runtime import (
        ActiveBoundaryAdapter,
        BaselineAdapter,
        GlobalInboxRuntime,
        TrackingSendDelegate,
    )
    from ...agent.send_coordinator import SendCoordinator

    client, adapter = point.client, _WsLocalContextAdapter()

    async def run_turn(planned):
        await bridge.dispatch_planned(holder["session"], planned)

    workspace = client.workspace or point.agent_cfg.resolve_workspace_dir()
    runtime = GlobalInboxRuntime(
        store=client.store,
        adapter=adapter,
        run_turn=run_turn,
        workspace=workspace,
        held_catchup=client.recover_pending_delivery,
        identity_aliases=(point.agent_id, client.slug),
        agent_id=point.agent_id,
        channel_audience_loader=lambda space_id, channel_id: read_channel_audience(
            space_id,
            channel_id,
            http=client.http,
            log=logger,
        ),
    )
    coordinator = SendCoordinator(
        slug=client.slug,
        keystore=client.keystore,
        http_client=client.http,
        data_client=InProcessDataClient(client.store, client),
        workspace=workspace,
        shared_workspace=str(shared_fs_dir()),
        baseline_source=BaselineAdapter(client.store),
        active_turn_source=ActiveBoundaryAdapter(client.store, runtime.active),
        held_recovery_source=runtime.held_recovery_source,
        channel_policy_source=client,
    )
    runtime.coordinator = coordinator
    runtime.send_delegate = TrackingSendDelegate(coordinator, runtime.attempts, runtime)
    runtime.register_autonomous_adoption()
    return runtime


def _install_owned_runtime(client, runtime) -> None:
    """Publish a connection-owned runtime on the long-lived client.

    The single install site, called only once the registry slot is claimed
    and the attach has begun, so ``_stop_ws_consumer``'s guarded reset is
    always reached. Installing earlier (at construction) left a rejected
    duplicate or a dropped ``connected`` write with an orphaned runtime that
    a later connection would reuse against a dead session (F1/B3).
    """
    # The ownership marker goes first: it is what ``_stop_ws_consumer``
    # guards on, so even a partial install is fully unwound.
    client._ws_local_owned_runtime = runtime
    client.global_runtime = runtime
    client.send_coordinator = runtime.coordinator
    client.send_delegate = runtime.send_delegate


class _ConnectedRelay:
    """One stable transport-connected callback per client.

    ``add_connected_callback`` is append-only, and the client outlives the
    session, so registering a fresh ``ReminderSync`` bound method on every
    attach would accumulate callbacks across attach/detach cycles. The relay
    is registered once and retargeted per attach instead.
    """

    def __init__(self) -> None:
        self._target = None

    def bind(self, target) -> None:
        self._target = target

    async def __call__(self) -> None:
        target = self._target
        if target is not None:
            await target()


async def _prepare_owned_reminder_sync(point: AttachPoint, client, runtime):
    """Give the owned runtime the worker's reminder-sync wiring.

    Without it the exposed reminder tools would schedule against a scheduler
    with no delivery authorizer and no server sync — a silently different
    contract from the same tools on the worker runtime.
    """
    from ...agent.reminder_sync import prepare_reminder_sync

    relay = getattr(client, "_ws_local_reminder_relay", None)
    if relay is None:
        relay = _ConnectedRelay()
        client._ws_local_reminder_relay = relay
        client.add_connected_callback(relay)
    return await prepare_reminder_sync(
        client, runtime, agent_id=point.agent_id, register_connected=relay.bind,
    )


async def _start_ws_consumer(
    hub: WsLocalHub, connection: dict, authed, on_message,
) -> None:
    from ...agent.global_inbox_runtime import await_listener_with_runtime

    point, client = hub.get(authed.slug), hub.get(authed.slug).client
    owned = connection.get("owned_runtime")
    heartbeat = asyncio.ensure_future(point.reporter.run_heartbeat_loop())
    reminder_sync = None
    reminder_task = None
    runtime_task = None
    try:
        if owned is not None:
            # First statement of the scope whose ``finally`` unwinds it.
            _install_owned_runtime(client, owned)
            reminder_sync = await _prepare_owned_reminder_sync(point, client, owned)
            reminder_task = asyncio.ensure_future(
                reminder_sync.run(request_snapshot_on_start=False)
            )
            runtime_task = asyncio.ensure_future(owned.run())
            await await_listener_with_runtime(
                client.listen(on_message),
                runtime_task,
                label=f"ws-local {authed.slug} global inbox runtime",
            )
        else:
            await client.listen(on_message)
    finally:
        await _stop_ws_consumer(
            point, client, owned, heartbeat, runtime_task,
            reminder_sync, reminder_task,
        )


async def _stop_ws_consumer(
    point: AttachPoint, client, owned, heartbeat, runtime_task,
    reminder_sync=None, reminder_task=None,
) -> None:
    point.reporter.stop()
    heartbeat.cancel()
    if owned is not None:
        owned.stop()
    if runtime_task is not None:
        runtime_task.cancel()
    if reminder_sync is not None:
        reminder_sync.stop()
        # The relay stays registered on the long-lived client; unbinding it
        # keeps a detached session's sync from being woken by a later connect.
        relay = getattr(client, "_ws_local_reminder_relay", None)
        if relay is not None:
            relay.bind(None)
    if reminder_task is not None:
        reminder_task.cancel()
    try:
        await heartbeat
    except asyncio.CancelledError:
        pass
    for task in (runtime_task, reminder_task):
        if task is None:
            continue
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if getattr(client, "_ws_local_owned_runtime", None) is owned and owned is not None:
        client.global_runtime = None
        client.send_coordinator = None
        client.send_delegate = None
        del client._ws_local_owned_runtime
