"""Pi RPC-mode Driver for ``@earendil-works/pi-coding-agent`` 0.84.3.

Pi is a persistent JSONL child (``pi --mode rpc``): commands in on stdin, one
JSON object per line; responses and events out on stdout.

Three properties of this protocol are load-bearing here:

* ``agent_settled`` is the sole turn terminal. In the shipped bundle it is
  emitted from a ``finally`` wrapping the whole prompt-and-continue loop, so it
  fires exactly once per run on every path, including abort and error.
  ``agent_end`` is not terminal -- retry, compaction, and queued continuations
  all run after it.
* Framing is LF-only. ``readline()`` on the byte stream is correct;
  ``str.splitlines()`` is not, because it also splits on U+2028/U+2029/U+0085,
  which are legal inside JSON strings (see ``tests/test_pi_rpc_conformance.py``).
* Pi ships no permission gate, so ``permission_bridge`` is ``False``. Extension
  UI dialogs are a different mechanism and are answered, not bridged; see
  ``_handle_extension_ui_request``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from typing import Any

from ....tasks import spawn
from ...errors import AgentAPIError
from ...provider_failures import provider_failure_message
from ..driver import (
    BusyDelivery,
    CancelCapability,
    CancelReceipt,
    CompactCapability,
    CompactReceipt,
    CompactRequest,
    ContextStatus,
    ContextStatusCapability,
    Driver,
    DriverCapabilities,
    HarnessEvent,
    HarnessEventType,
    InputReceipt,
    PermissionDecision,
    PermissionRef,
    ProtocolDiagnostics,
    RuntimeLifecycle,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    SteerCapability,
    TurnInput,
    TurnRef,
    TurnStarted,
    UnsupportedCapability,
)
from .pi_bridge import (
    BRIDGE_NONCE_ENV,
    BRIDGE_READY_FILE_ENV,
    await_bridge_ready,
    clear_ready_file,
)
from ..support.jsonl_rpc import (
    RpcFrameTooLarge,
    RpcRequestTimeout,
    await_rpc_response,
    decode_json_object,
    fail_pending_requests,
    read_json_line,
    write_json_line,
)
from ..support.subprocess_io import (
    drain_subprocess_stream,
    process_group_spawn_kwargs,
    shutdown_process_tree,
)
from .pi_protocol import (
    EXTENSION_UI_DIALOG_METHODS,
    EXTENSION_UI_FIRE_AND_FORGET_METHODS,
    EXTENSION_UI_REQUEST,
    RESPONSE_FRAME,
    TERMINAL_EVENT,
    build_pi_launch_command,
    normalize_pi_event,
)

logger = logging.getLogger(__name__)

_STREAM_READER_LIMIT_BYTES = 16 * 1024 * 1024
_SHUTDOWN_GRACE_SECONDS = 3.0

PI_CAPABILITIES = DriverCapabilities(
    session_resume=True,
    inflight_turn_recovery=False,
    # docs/rpc.md: a steering message is delivered "after the current assistant
    # turn finishes executing its tool calls, before the next LLM call" --
    # queued to a boundary, not injected into the running request.
    steer=SteerCapability.GATED,
    cancel=CancelCapability.TYPED,
    # get_session_stats.contextUsage is the authoritative context estimate and
    # must be asked for; the usage on message_update is cumulative provider
    # usage, a different quantity.
    context_status=ContextStatusCapability.PULL,
    compact=CompactCapability.TYPED,
    permission_bridge=False,
    lifecycle=RuntimeLifecycle.PERSISTENT_CHILD,
    busy_delivery=BusyDelivery.STEER,
)


# Pi's config directory, default ``~/.pi/agent``. The per-agent value must be
# set explicitly: falling back to the default would put every agent in the
# host user's Pi home.
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"

# Auto-discovered extension providing Puffo's tools. Pi ships no MCP
# (README.md: "No MCP."), so this extension is the only in-process way for a
# Pi agent to call send_message / read_inbox.
PUFFO_PI_EXTENSION = "puffo-tools"


class PiToolBridgeUnavailableError(RuntimeError):
    """Pi cannot participate in Puffo without an installed tool bridge."""


def verify_pi_tool_bridge(spec: RuntimeSpec) -> str:
    """Return the extension backing Puffo tools, or refuse to admit Pi.

    A Pi agent with skills but no tool bridge can read its instructions and
    produce text that nobody receives: ``send_message`` is how an agent
    participates at all, and Pi exposes no MCP to carry it. Admitting that
    configuration would look like a working runtime and behave like a mute one,
    so this refuses instead of degrading.
    """
    if spec.mcp_servers:
        # Pi has no MCP slot to put these in. Dropping them silently is what
        # would produce the mute agent described above.
        raise PiToolBridgeUnavailableError(
            "Pi does not support MCP; "
            f"{len(spec.mcp_servers)} MCP server(s) cannot be projected"
        )
    agent_dir = str(spec.environment.get(PI_AGENT_DIR_ENV) or "")
    if not agent_dir:
        raise PiToolBridgeUnavailableError(
            f"{PI_AGENT_DIR_ENV} is unset; refusing to fall back to the host "
            "user's ~/.pi/agent"
        )
    extensions = os.path.join(agent_dir, "extensions")
    for candidate in (
        os.path.join(extensions, f"{PUFFO_PI_EXTENSION}.ts"),
        os.path.join(extensions, PUFFO_PI_EXTENSION, "index.ts"),
    ):
        if os.path.isfile(candidate):
            return candidate
    raise PiToolBridgeUnavailableError(
        f"no Puffo tool bridge in {extensions}: expected "
        f"{PUFFO_PI_EXTENSION}.ts or {PUFFO_PI_EXTENSION}/index.ts"
    )


class PiDriver(Driver):
    """Persistent ``pi --mode rpc`` child speaking pinned 0.84.3 JSONL."""

    def __init__(
        self,
        process_factory: Callable[[RuntimeSpec], Any] | None = None,
        *,
        executable_version: str = "",
        request_timeout_seconds: float = 60.0,
        bridge_ready_timeout: float = 20.0,
    ):
        self.process_factory = process_factory
        self.executable_version = executable_version
        self.request_timeout_seconds = request_timeout_seconds
        self.bridge_ready_timeout = bridge_ready_timeout
        self._proc: Any = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._request_id = 0
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._runtime_ref = RuntimeRef(f"runtime_{uuid.uuid4().hex}")
        self._session_ref = SessionRef("")
        self._native_session_id = ""
        self._active = TurnRef("")
        self._context = ContextStatus(stale=True)
        # Accumulated across the whole agent run and reported once, at
        # agent_settled. Pi reports failures as separate events that do not end
        # the run, so the terminal outcome is only known when the run settles.
        self._turn_outcome = "succeeded"
        self._turn_usage: dict[str, int] = {}
        self._closed = False

    def current_capabilities(self) -> DriverCapabilities:
        return PI_CAPABILITIES

    # -- lifecycle ---------------------------------------------------------

    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened:
        if self._proc is not None:
            raise RuntimeError("driver is already open")
        if self._closed:
            self._prepare_reopen()
        verify_pi_tool_bridge(spec)
        ready_file = spec.environment.get(BRIDGE_READY_FILE_ENV)
        nonce = spec.environment.get(BRIDGE_NONCE_ENV)
        if ready_file:
            # Before the child, not after: the nonce lives in the spec and
            # survives a restart, so last run's file would otherwise attest a
            # bridge this process never loaded.
            clear_ready_file(Path(ready_file))
        await self._start_process(spec)
        self._reader = spawn(self._read_loop(), name="pi.read_loop")
        self._stderr_reader = spawn(
            drain_subprocess_stream(getattr(self._proc, "stderr", None)),
            name="pi.stderr_reader",
        )
        resumed = False
        if resume is not None and str(resume):
            resumed = await self._switch_session(str(resume))
        stats = await self._request("get_session_stats", {})
        native = str(stats.get("sessionFile") or "")
        if not native:
            # Pi reports no session file when persistence is off. A driver
            # declaring session_resume=True cannot honour it in that mode, so
            # fail loudly instead of resuming nothing on every restart.
            raise RuntimeError(
                "Pi reported no sessionFile; session persistence is disabled "
                "(--no-session), which is incompatible with session_resume"
            )
        self._native_session_id = native
        self._session_ref = SessionRef(native)
        self._context = _context_from_stats(stats)
        await self._require_loaded_bridge(ready_file, nonce)
        await self._emit(
            HarnessEventType.SESSION_RESUMED
            if resumed
            else HarnessEventType.SESSION_OPENED,
            native_payload=stats,
        )
        return RuntimeOpened(
            self._runtime_ref,
            self._session_ref,
            native,
            resumed,
            PI_CAPABILITIES,
            ProtocolDiagnostics(
                executable_version=self.executable_version,
                schema_source="pinned",
                native_capabilities=(
                    "prompt",
                    "steer",
                    "abort",
                    "compact",
                    "switch_session",
                    "get_session_stats",
                ),
                warnings=("pi_has_no_mcp",),
            ),
        )

    async def _require_loaded_bridge(
        self, ready_file: str | None, nonce: str | None
    ) -> None:
        """Refuse a session whose bridge did not actually load.

        ``verify_pi_tool_bridge`` proves an installer ran. It cannot see an
        extension that threw on load, was disabled, or belongs to a previous
        run -- all of which leave the file in place and the agent unable to
        send a message. Only this spawn's nonce settles that.
        """
        if not ready_file or not nonce:
            await self.close()
            raise PiToolBridgeUnavailableError(
                f"{BRIDGE_READY_FILE_ENV}/{BRIDGE_NONCE_ENV} are unset; "
                "bridge readiness cannot be attested for this spawn"
            )
        tools = await await_bridge_ready(
            Path(ready_file), nonce, timeout_seconds=self.bridge_ready_timeout
        )
        if tools is None:
            await self.close()
            raise PiToolBridgeUnavailableError(
                "the Puffo tool bridge did not attest readiness for this "
                "spawn; the extension is installed but did not load"
            )
        logger.info("pi bridge attested %d Puffo tool(s)", tools)

    async def _start_process(self, spec: RuntimeSpec) -> None:
        if self.process_factory is None:
            command = build_pi_launch_command(spec)
            self._proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.workspace_dir or None,
                # The whole child environment, never merged into the ambient
                # one: build_child_environment sanitizes by omission, and an
                # omitted key carries no instruction to delete.
                env=dict(spec.environment),
                limit=_STREAM_READER_LIMIT_BYTES,
                **process_group_spawn_kwargs(),
            )
        else:
            self._proc = self.process_factory(spec)
            if asyncio.iscoroutine(self._proc):
                self._proc = await self._proc

    async def _switch_session(self, session_path: str) -> bool:
        """Resume a session file, treating an extension veto as a failure.

        ``switch_session`` answers ``success: true`` with ``data.cancelled:
        true`` when a ``session_before_switch`` handler vetoes it. Reading only
        ``success`` would report a resume that never happened and silently
        continue in the wrong session.
        """
        data = await self._request(
            "switch_session", {"sessionPath": session_path}
        )
        if isinstance(data, dict) and data.get("cancelled"):
            raise AgentAPIError(
                "Pi switch_session was cancelled by an extension handler.",
                is_auth=False,
                error_code="invalid_resume",
            )
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None:
            await shutdown_process_tree(
                proc,
                waiter=None,
                timeout=_SHUTDOWN_GRACE_SECONDS,
                task_name="pi.process_wait",
            )
        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._reader = None
        self._stderr_reader = None
        fail_pending_requests(self._pending, "Pi RPC session closed")
        self._active = TurnRef("")
        self._reset_turn()
        self._context = ContextStatus(stale=True)
        await self._events.put(None)

    def _prepare_reopen(self) -> None:
        self._closed = False
        self._events = asyncio.Queue()
        self._pending.clear()
        self._active = TurnRef("")
        self._reset_turn()
        self._context = ContextStatus(stale=True)

    # -- turn commands -----------------------------------------------------

    async def start_turn(self, input: TurnInput):
        if self._active.value:
            raise RuntimeError("one turn is already active")
        local = TurnRef(f"turn_{uuid.uuid4().hex}")
        self._active = local
        self._reset_turn()
        try:
            # No streamingBehavior: this path is only valid while idle, and Pi
            # rejects a bare prompt during streaming rather than guessing.
            await self._request("prompt", {"message": input.content})
        except BaseException:
            self._active = TurnRef("")
            self._reset_turn()
            raise
        return TurnStarted(local, local.value)

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        self._require_active(turn)
        await self._request("steer", {"message": input.content})
        # GATED, not CURRENT_TURN: Pi queues this to the next tool-call
        # boundary, so acceptance is not delivery into the running request.
        return InputReceipt(
            True, turn, input.client_correlation_id, delivery="gated"
        )

    async def cancel_turn(self, turn: TurnRef):
        self._require_active(turn)
        self._turn_outcome = "cancelled"
        await self._request("abort", {})
        # The terminal still arrives as agent_settled; abort does not end the
        # turn by itself. Emitting one here would produce two terminals.
        return CancelReceipt(True, turn)

    async def context_status(self):
        if self._proc is None:
            return self._context
        try:
            stats = await self._request("get_session_stats", {})
        except (AgentAPIError, RuntimeError):
            # A pull that fails leaves the last reading in place, marked stale,
            # rather than reporting a fabricated zero.
            return ContextStatus(
                used_tokens=self._context.used_tokens,
                context_window=self._context.context_window,
                stale=True,
                measured_at=self._context.measured_at,
            )
        self._context = _context_from_stats(stats)
        return self._context

    async def compact(self, request: CompactRequest):
        if self._active.value:
            raise RuntimeError("compaction requires an idle session")
        params: dict[str, Any] = {}
        if request.instructions:
            params["customInstructions"] = request.instructions
        await self._request("compact", params)
        # Pi answers compact synchronously; there is no separate operation to
        # poll, so no operation_ref is invented for callers to track.
        return CompactReceipt(True, "")

    async def resolve_permission(
        self, request: PermissionRef, decision: PermissionDecision
    ):
        # Pi ships no permission gate (docs: "intentionally does not include"
        # a permission popup), so there is nothing to resolve. Reported as an
        # unsupported capability rather than a silent success.
        return UnsupportedCapability(
            "permission_bridge",
            "Pi exposes no permission gate; nothing to resolve",
        )

    def events(self) -> AsyncIterator[HarnessEvent]:
        async def iterate():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return iterate()

    # -- transport ---------------------------------------------------------

    async def _request(self, command: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = f"puffo-{self._request_id}"
        try:
            return await await_rpc_response(
                self._pending,
                request_id,
                send=self._write({"id": request_id, "type": command, **params}),
                timeout_seconds=self.request_timeout_seconds,
            )
        except RpcRequestTimeout:
            logger.warning(
                "Pi command %s timed out after %ss",
                command,
                f"{self.request_timeout_seconds:g}",
            )
            raise AgentAPIError(
                f"{provider_failure_message('provider_unavailable')} "
                f"Pi command {command} timed out after "
                f"{self.request_timeout_seconds:g}s.",
                is_auth=False,
                error_code="provider_unavailable",
            ) from None

    async def _write(self, frame: dict[str, Any]) -> None:
        await write_json_line(self._proc.stdin, self._write_lock, frame)

    async def _read_loop(self) -> None:
        proc = self._proc
        try:
            while True:
                try:
                    line = await read_json_line(self._proc.stdout)
                except RpcFrameTooLarge:
                    # A frame past the stream limit leaves stdout partially
                    # consumed; the session cannot be resynchronized.
                    logger.warning("pi rpc frame exceeded the stdout limit")
                    break
                if not line:
                    break
                # LF-only framing; strip an optional CR as docs/rpc.md allows.
                record = line.rstrip(b"\r\n")
                if not record:
                    # A blank record is framing, not a frame. Reporting it as
                    # protocol_parse would tell the manager the peer is
                    # speaking badly when it has said nothing at all.
                    continue
                try:
                    frame = decode_json_object(record)
                except (UnicodeDecodeError, ValueError):
                    await self._emit(
                        HarnessEventType.RUNTIME_WARNING,
                        data={"code": "protocol_parse"},
                    )
                    continue
                except TypeError:
                    await self._emit(
                        HarnessEventType.RUNTIME_WARNING,
                        data={"code": "protocol_frame"},
                    )
                    continue
                try:
                    await self._dispatch_frame(frame)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad frame must not kill the reader and orphan every
                    # pending request.
                    logger.exception("pi frame dispatch failed")
                    await self._emit(
                        HarnessEventType.RUNTIME_WARNING,
                        data={"code": "frame_dispatch_failed"},
                    )
        finally:
            fail_pending_requests(self._pending, "Pi RPC session exited")
            if not self._closed:
                # A deliberate close() already reports the shutdown; emitting
                # here too would give the manager a second, spurious exit.
                await self._emit(
                    HarnessEventType.RUNTIME_EXITED,
                    data={"returncode": _returncode(proc)},
                )

    async def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        type_ = str(frame.get("type") or "")
        if type_ == RESPONSE_FRAME:
            await self._resolve_response(frame)
            return
        if type_ == EXTENSION_UI_REQUEST:
            await self._handle_extension_ui_request(frame)
            return
        if type_ == TERMINAL_EVENT:
            await self._complete_turn(frame)
            return
        if type_ == "agent_start" and not self._active.value:
            self._active = TurnRef(f"turn_{uuid.uuid4().hex}")
            self._reset_turn()
        for event in normalize_pi_event(
            frame,
            session_ref=self._session_ref,
            turn_ref=self._active if self._active.value else None,
            native_session_id=self._native_session_id,
            native_turn_id=self._active.value,
        ):
            if event.type == HarnessEventType.CONTEXT_UPDATED:
                self._turn_usage = dict(event.data)
            if _is_failure_signal(event):
                self._turn_outcome = "failed"
            await self._events.put(event)

    async def _resolve_response(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, str):
            # Parse errors answer with command "parse" and no id; there is no
            # caller to resolve, so surface it rather than dropping it.
            await self._emit(
                HarnessEventType.RUNTIME_WARNING,
                data={
                    "code": "unmatched_response",
                    "command": str(frame.get("command") or ""),
                },
            )
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if frame.get("success"):
            future.set_result(frame.get("data") or {})
            return
        future.set_exception(
            AgentAPIError(
                f"Pi command {frame.get('command') or 'unknown'} failed.",
                is_auth=False,
                error_code="provider_error",
            )
        )

    async def _handle_extension_ui_request(self, frame: dict[str, Any]) -> None:
        """Answer blocking extension dialogs; never answer fire-and-forget ones.

        A dialog method blocks the Pi agent until the client replies, so an
        unanswered one is a stall, not a no-op. Puffo has no operator at this
        layer and choosing an option would be a policy decision made in the
        transport, so every dialog is cancelled: the extension receives
        ``undefined`` (or ``false`` for ``confirm``) and the run continues.
        Answering a fire-and-forget method would instead send an id the agent
        is not waiting on.
        """
        method = str(frame.get("method") or "")
        request_id = frame.get("id")
        known = (
            method in EXTENSION_UI_DIALOG_METHODS
            or method in EXTENSION_UI_FIRE_AND_FORGET_METHODS
        )
        await self._emit(
            HarnessEventType.RUNTIME_WARNING,
            turn_ref=self._active if self._active.value else None,
            data={
                "code": "extension_ui_request"
                if known
                else "unknown_extension_ui_method",
                "method": method or "unknown",
                "answered": method in EXTENSION_UI_DIALOG_METHODS,
            },
            native_payload=frame,
        )
        if method in EXTENSION_UI_DIALOG_METHODS and isinstance(request_id, str):
            await self._write(
                {
                    "type": "extension_ui_response",
                    "id": request_id,
                    "cancelled": True,
                }
            )

    async def _complete_turn(self, frame: dict[str, Any]) -> None:
        """Emit the single terminal for the settled agent run."""
        if not self._active.value:
            # A settled run the daemon did not start: an extension or a queued
            # continuation woke the model after our turn was finalized. Report
            # it rather than dropping it, so the daemon can bind a turn to it.
            await self._emit(
                HarnessEventType.AUTONOMOUS_COMPLETED,
                data={"outcome": "succeeded"},
                native_payload=frame,
            )
            return
        data: dict[str, Any] = {"outcome": self._turn_outcome}
        data.update(self._turn_usage)
        turn = self._active
        self._active = TurnRef("")
        self._reset_turn()
        await self._emit(
            HarnessEventType.TURN_COMPLETED,
            turn_ref=turn,
            data=data,
            native_payload=frame,
        )

    async def _emit(
        self,
        type_: HarnessEventType,
        *,
        turn_ref: TurnRef | None = None,
        data: dict[str, Any] | None = None,
        native_payload: Any = None,
    ) -> None:
        await self._events.put(
            HarnessEvent.normalized(
                type=type_,
                driver="pi",
                session_ref=self._session_ref,
                turn_ref=turn_ref,
                native_session_id=self._native_session_id,
                native_turn_id=(turn_ref.value if turn_ref is not None else ""),
                data=data or {},
                native_payload=native_payload,
            )
        )

    def _reset_turn(self) -> None:
        self._turn_outcome = "succeeded"
        self._turn_usage = {}

    def _require_active(self, turn: TurnRef) -> None:
        if not self._active.value or turn.value != self._active.value:
            raise RuntimeError("unknown or stale turn reference")


def _is_failure_signal(event: HarnessEvent) -> bool:
    """A retry or compaction that finally failed makes the run's outcome bad.

    Pi reports these as standalone events; the run continues and settles
    normally afterwards, so the outcome has to be remembered until the terminal.
    """
    if event.type != HarnessEventType.RUNTIME_WARNING:
        return False
    data = event.data
    return (
        data.get("code") == "auto_retry_end" and not data.get("succeeded", True)
    )


def _context_from_stats(stats: Any) -> ContextStatus:
    """Read ``contextUsage``; absent or null fields mean stale, not zero.

    docs/rpc.md: ``contextUsage`` is omitted when no model or context window is
    available, and its ``tokens``/``percent`` are null right after compaction
    until a fresh assistant response supplies usage.
    """
    if not isinstance(stats, dict):
        return ContextStatus(stale=True)
    usage = stats.get("contextUsage")
    if not isinstance(usage, dict):
        return ContextStatus(stale=True)
    tokens = _optional_int(usage.get("tokens"))
    window = _optional_int(usage.get("contextWindow"))
    return ContextStatus(
        used_tokens=tokens,
        context_window=window,
        stale=tokens is None,
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _returncode(proc: Any) -> int | None:
    code = getattr(proc, "returncode", None)
    return code if isinstance(code, int) else None
