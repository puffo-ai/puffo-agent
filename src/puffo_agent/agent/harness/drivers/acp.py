"""Generic ACP v1 Driver backed by the official Python SDK."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.connection import StreamDirection, StreamEvent
from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    Implementation,
    McpServerStdio,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    WaitForTerminalExitResponse,
)
from ....tasks import spawn
from ...cli_bin import normalize_launch_argv
from ..support.cleanup_errors import (
    CLEANUP_TIMEOUT_SECONDS,
    collect_cleanup_errors,
    raise_collected_errors,
)
from ...provider_failures import PROVIDER_FAILURES, classify_provider_failure
from ..driver import (
    BusyDelivery,
    CancelCapability,
    CancelReceipt,
    CompactCapability,
    CompactRequest,
    ContextStatusCapability,
    Driver,
    DriverCapabilities,
    HarnessEvent,
    HarnessEventType,
    McpServerSpec,
    PermissionDecision,
    PermissionReceipt,
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
from ..driver_authority_server import (
    DRIVER_AUTHORITY_FD_ENV,
    DriverAuthorityServer,
)
from ..support.subprocess_io import (
    drain_subprocess_stream_keeping_tail,
    process_group_spawn_kwargs,
    shutdown_process_tree,
)


@dataclass(frozen=True, slots=True)
class AcpLaunchPlan:
    """Complete ACP process and session inputs presented to one validator.

    ``argv`` is the authoritative execution vector. ``executable`` records
    the unresolved source value for policy/audit purposes; validators that
    constrain what will run must inspect ``argv`` as well.

    The plan intentionally captures values, not filesystem identities. The
    producer of ``RuntimeSpec`` remains responsible for any stronger
    ``lstat``/device/inode or symlink-stability guarantee required by policy.
    """

    executable: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    cwd: str
    mcp_servers: tuple[Any, ...]


_VALIDATED_LAUNCH_TOKEN = object()
_SHUTDOWN_GRACE_SECONDS = 3.0


class ValidatedLaunchPlan:
    """An ACP launch plan sealed at the final pre-spawn validation seam.

    This means all five inputs were assembled, frozen, and presented to an
    injected validator when one exists; it does not claim built-in content
    validation. Construction is deliberately guarded against accidental
    bypass. A determined caller could explicitly import the private module
    token, but such a bypass is grep-visible and reviewable.
    """

    __slots__ = ("plan",)

    def __init__(self, plan: AcpLaunchPlan, *, _token: object) -> None:
        if _token is not _VALIDATED_LAUNCH_TOKEN:
            raise TypeError(
                "ValidatedLaunchPlan can only be created by AcpDriver"
            )
        self.plan = plan


# JSON-RPC code of the ACP SDK's RequestError.auth_required() — the only
# structured error class ACP v1 defines; everything else arrives as free
# text and goes through the shared provider-failure classifier.
AUTH_REQUIRED_CODE = -32000


def acp_capabilities(*, session_resume: bool) -> DriverCapabilities:
    return DriverCapabilities(
        session_resume=session_resume,
        inflight_turn_recovery=False,
        steer=SteerCapability.NONE,
        cancel=CancelCapability.TYPED,
        context_status=ContextStatusCapability.PUSH,
        compact=CompactCapability.NONE,
        permission_bridge=True,
        lifecycle=RuntimeLifecycle.PERSISTENT_CHILD,
        busy_delivery=BusyDelivery.REJECT,
    )


class _PuffoAcpClient:
    def __init__(self, driver: AcpDriver) -> None:
        self.driver = driver

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        return await self.driver._request_permission(
            options, session_id, tool_call
        )

    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        await self.driver._session_update(session_id, update)

    async def write_text_file(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, **kwargs: Any) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, **kwargs: Any) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> ReleaseTerminalResponse:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        await self.driver._unsupported_extension(method)
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        await self.driver._unsupported_extension(method)

    def on_connect(self, conn: Any) -> None:
        return None


class AcpDriver(Driver):
    """Persistent ACP agent process with negotiated session semantics.

    This is the only trusted entrypoint for constrained LingTai
    (``puffo-v0`` / ``puffo-v1``) identity binding.
    The complete process/session launch plan is validated immediately before
    spawn; the separate OpenCode driver is explicitly outside that contract.
    """

    static_steer_capability = SteerCapability.NONE

    def __init__(
        self,
        process_factory: Callable[..., Any] | None = None,
        *,
        connection_factory: Callable[..., Any] = connect_to_agent,
        executable_version: str = "",
        launch_validator: Callable[[AcpLaunchPlan], None] | None = None,
    ) -> None:
        self.process_factory = process_factory
        self.connection_factory = connection_factory
        self.executable_version = executable_version
        self.launch_validator = launch_validator
        self._proc: Any = None
        self._conn: Any = None
        self._watcher: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[bytes] | None = None
        self._runtime_ref = RuntimeRef(f"runtime_{uuid.uuid4().hex}")
        self._session_ref = SessionRef("")
        self._native_session_id = ""
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._prompt_task: asyncio.Task[None] | None = None
        self._prompt_sent: asyncio.Future[None] | None = None
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._permissions: dict[
            PermissionRef,
            tuple[asyncio.Future[PermissionDecision], list[PermissionOption]],
        ] = {}
        self._output_blocks: set[str] = set()
        self._fallback_block_id = ""
        self._capabilities = acp_capabilities(session_resume=False)
        self._model_selection = ""
        self._spawn_warnings: tuple[str, ...] = ()
        self._driver_authority: DriverAuthorityServer | None = None
        self._closed = False

    def current_capabilities(self) -> DriverCapabilities:
        return self._capabilities

    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened:
        if self._proc is not None:
            raise RuntimeError("driver is already open")
        if not spec.executable:
            raise RuntimeError("ACP driver requires an explicit executable")
        if self._closed:
            self._closed = False
            self._events = asyncio.Queue()
        launch = self._validate_launch_plan(spec)
        self._proc = await self._spawn(launch)
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("ACP child did not expose stdio pipes")
        self._stderr_reader = spawn(
            drain_subprocess_stream_keeping_tail(self._proc.stderr),
            name="acp.stderr",
        )
        client = _PuffoAcpClient(self)
        self._conn = self.connection_factory(
            client,
            self._proc.stdin,
            self._proc.stdout,
            observers=[self._observe_stream],
        )
        initialized = await self._conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(
                    read_text_file=False,
                    write_text_file=False,
                ),
                terminal=False,
            ),
            client_info=Implementation(name="puffo-agent", version="2"),
        )
        if initialized.protocol_version != PROTOCOL_VERSION:
            raise RuntimeError(
                "ACP agent negotiated unsupported protocol version "
                f"{initialized.protocol_version}"
            )
        native_caps = initialized.agent_capabilities
        can_load = bool(native_caps and native_caps.load_session)
        self._capabilities = acp_capabilities(session_resume=can_load)
        if resume is not None:
            if not can_load:
                raise RuntimeError("ACP agent does not support session/load")
            await self._conn.load_session(
                cwd=launch.plan.cwd,
                session_id=str(resume),
                mcp_servers=list(launch.plan.mcp_servers),
            )
            native_session_id = str(resume)
            resumed = True
        else:
            session = await self._conn.new_session(
                cwd=launch.plan.cwd,
                mcp_servers=list(launch.plan.mcp_servers),
            )
            native_session_id = session.session_id
            resumed = False
        if not native_session_id:
            raise RuntimeError("ACP session response omitted sessionId")
        self._native_session_id = native_session_id
        self._session_ref = SessionRef(native_session_id)
        self._watcher = spawn(
            self._watch_process(self._proc), name="acp.watch"
        )
        await self._emit(
            HarnessEventType.SESSION_RESUMED
            if resumed
            else HarnessEventType.SESSION_OPENED
        )
        capability_names = ["cancel", "permission_bridge"]
        if can_load:
            capability_names.append("session/load")
        if self._model_selection:
            capability_names.append(f"model_selection/{self._model_selection}")
        return RuntimeOpened(
            self._runtime_ref,
            self._session_ref,
            native_session_id,
            resumed,
            self._capabilities,
            ProtocolDiagnostics(
                executable_version=self.executable_version,
                schema_source="agent-client-protocol==0.10.1/protocol-v1",
                native_capabilities=tuple(capability_names),
                warnings=self._spawn_warnings,
            ),
        )

    def _validate_launch_plan(self, spec: RuntimeSpec) -> ValidatedLaunchPlan:
        """Assemble, seal, and optionally validate every launch input."""
        model_args: tuple[str, ...] = ()
        self._model_selection = ""
        self._spawn_warnings = ()
        if spec.model:
            if operator_pinned_model(spec.launch_args):
                self._model_selection = "operator_launch_args"
                self._spawn_warnings = (
                    "launch_args already pin a model flag; spec.model "
                    f"{spec.model!r} was not applied",
                )
            else:
                model_args = model_launch_args(spec.executable, spec.model)
                if model_args:
                    self._model_selection = "launch_preset"
                else:
                    self._spawn_warnings = (
                        "model selection is not supported for this ACP "
                        f"executable; spec.model {spec.model!r} was dropped "
                        "and the agent runs its default model",
                    )
        argv = (
            *normalize_launch_argv(spec.executable),
            *spec.launch_args,
            *model_args,
        )
        plan = AcpLaunchPlan(
            executable=spec.executable,
            argv=argv,
            environment=MappingProxyType(dict(spec.environment)),
            cwd=spec.workspace_dir,
            # The Driver is transport, not policy: it forwards whatever
            # the runtime projected into ``spec.mcp_servers``, converted to
            # the ACP wire shape. Which profiles receive Puffo's server —
            # and that puffo-v0 stays empty, since it rejects a non-empty
            # ``mcpServers`` at ``session/new`` — is decided by
            # ``_project_protocol_mcp`` in the runtime.
            mcp_servers=tuple(
                _to_acp_stdio_server(server)
                for server in spec.mcp_servers
            ),
        )
        if self.launch_validator is not None:
            self.launch_validator(plan)
        return ValidatedLaunchPlan(plan, _token=_VALIDATED_LAUNCH_TOKEN)

    async def _spawn(self, launch: ValidatedLaunchPlan) -> Any:
        if not isinstance(launch, ValidatedLaunchPlan):
            raise TypeError("ACP spawn requires a ValidatedLaunchPlan")
        plan = launch.plan
        environment = dict(plan.environment)
        # This is a Driver-owned carrier, never caller-supplied ambient state.
        caller_supplied_authority = environment.pop(
            DRIVER_AUTHORITY_FD_ENV, None
        )
        uses_driver_authority = _uses_lingtai_driver_authority(plan.argv)
        if self.process_factory is not None:
            if uses_driver_authority:
                raise RuntimeError(
                    "constrained LingTai ACP requires the POSIX local spawn path"
                )
            # One call with the declared signature. Retrying on TypeError
            # cannot distinguish wrong arity from an internal factory failure
            # and could spawn a second child on the error path.
            sanitized_plan = (
                replace(plan, environment=MappingProxyType(environment))
                if caller_supplied_authority is not None
                else plan
            )
            proc = self.process_factory(plan.argv, sanitized_plan)
            return await proc if asyncio.iscoroutine(proc) else proc
        if not uses_driver_authority:
            return await asyncio.create_subprocess_exec(
                *plan.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=plan.cwd or None,
                env=environment,
                limit=16 * 1024 * 1024,
                **process_group_spawn_kwargs(),
            )

        if os.name != "posix":
            raise RuntimeError(
                "constrained LingTai ACP requires a POSIX local spawn path"
            )
        authority = DriverAuthorityServer()
        endpoint = authority.issue_root(launch_id=str(self._runtime_ref))
        endpoint_fd = endpoint.fileno()
        environment[DRIVER_AUTHORITY_FD_ENV] = str(endpoint_fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *plan.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=plan.cwd or None,
                env=environment,
                limit=16 * 1024 * 1024,
                pass_fds=(endpoint_fd,),
                **process_group_spawn_kwargs(),
            )
        except BaseException:
            authority.close()
            raise
        finally:
            endpoint.close()
        self._driver_authority = authority
        return proc

    async def start_turn(self, input: TurnInput):
        if self._conn is None:
            raise RuntimeError("driver is not open")
        if self._active.value:
            raise RuntimeError("one turn is already active")
        turn = TurnRef(f"turn_{uuid.uuid4().hex}")
        native_turn = input.client_correlation_id or str(uuid.uuid4())
        self._active = turn
        self._active_native_turn_id = native_turn
        self._output_blocks.clear()
        self._fallback_block_id = f"assistant_{uuid.uuid4().hex}"
        self._prompt_sent = asyncio.get_running_loop().create_future()
        prompt_sent = self._prompt_sent
        prompt_task = spawn(
            self._run_prompt(turn, input.content), name="acp.prompt"
        )
        self._prompt_task = prompt_task
        sent_wait = spawn(
            asyncio.shield(prompt_sent), name="acp.prompt_admission"
        )
        done, _ = await asyncio.wait(
            {sent_wait, prompt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if prompt_task in done and not prompt_sent.done():
            await prompt_task
            raise RuntimeError("ACP prompt completed before request admission")
        await sent_wait
        return TurnStarted(
            turn,
            native_turn_id=native_turn,
            accepted=True,
            delivery="jsonrpc_request_written",
        )

    async def _run_prompt(self, turn: TurnRef, content: str) -> None:
        try:
            response = await self._conn.prompt(
                session_id=self._native_session_id,
                prompt=[text_block(content)],
            )
        except asyncio.CancelledError:
            raise
        except RequestError as exc:
            error_obj = exc.to_error_obj()
            detail = " ".join(
                str(part)
                for part in (error_obj.get("message"), error_obj.get("data"))
                if part
            )
            if exc.code == AUTH_REQUIRED_CODE:
                # The one structured code ACP gives us; everything else is
                # free text through the shared classifier.
                error_code = "authentication"
            else:
                error_code = classify_provider_failure(
                    status=None, diagnostic=detail
                )
            failure = PROVIDER_FAILURES.get(error_code)
            await self._finish_turn(
                turn,
                HarnessEventType.TURN_ABANDONED,
                {
                    "outcome": "abandoned",
                    "error_code": error_code,
                    "retryable": failure.retryable if failure else True,
                    # The raw agent diagnostic used to be dropped here,
                    # leaving nothing to debug from. Bounded, not gone.
                    "diagnostic": detail[:2000],
                },
            )
            return
        except Exception as exc:
            await self._finish_turn(
                turn,
                HarnessEventType.TURN_ABANDONED,
                {
                    "outcome": "abandoned",
                    "error_code": "acp_prompt_failed",
                    "retryable": True,
                    "diagnostic": repr(exc)[:2000],
                },
            )
            return
        stop_reason = str(response.stop_reason)
        outcome = "succeeded" if stop_reason == "end_turn" else "failed"
        if stop_reason == "cancelled":
            event_type = HarnessEventType.TURN_ABANDONED
            data = {
                "outcome": "abandoned",
                "error_code": "cancelled",
                "retryable": False,
            }
        else:
            event_type = HarnessEventType.TURN_COMPLETED
            data = {"outcome": outcome, "stop_reason": stop_reason}
        await self._finish_turn(turn, event_type, data)

    async def _finish_turn(
        self,
        turn: TurnRef,
        type_: HarnessEventType,
        data: dict[str, Any],
    ) -> None:
        if turn != self._active:
            return
        for block_id in tuple(self._output_blocks):
            await self._emit(
                HarnessEventType.ASSISTANT_COMPLETED,
                turn=turn,
                data={"block_id": block_id},
            )
        for future, _ in self._permissions.values():
            if not future.done():
                future.set_result(PermissionDecision.DENY)
        self._permissions.clear()
        terminal = self._event(type_, turn=turn, data=data)
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._prompt_sent = None
        self._prompt_task = None
        self._output_blocks.clear()
        await self._events.put(terminal)

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn: TurnRef):
        self._require_active(turn)
        await self._conn.cancel(session_id=self._native_session_id)
        return CancelReceipt(True, turn)

    async def context_status(self):
        return UnsupportedCapability("context_status", "ACP context is push-only")

    async def compact(self, request: CompactRequest):
        return UnsupportedCapability("compact")

    async def resolve_permission(
        self, request: PermissionRef, decision: PermissionDecision
    ):
        pending = self._permissions.get(request)
        if pending is None:
            raise RuntimeError("unknown or stale permission reference")
        future, _ = pending
        if not future.done():
            future.set_result(decision)
        return PermissionReceipt(True, request)

    def events(self) -> AsyncIterator[HarnessEvent]:
        async def iterate():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return iterate()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future, _ in self._permissions.values():
            if not future.done():
                future.set_result(PermissionDecision.DENY)
        self._permissions.clear()
        errors: list[BaseException] = []
        if self._conn is not None:
            await collect_cleanup_errors(
                self._conn.close(), errors, timeout=CLEANUP_TIMEOUT_SECONDS
            )
            self._conn = None
        proc, self._proc = self._proc, None
        await collect_cleanup_errors(
            shutdown_process_tree(
                proc,
                waiter=self._watcher,
                timeout=_SHUTDOWN_GRACE_SECONDS,
                task_name="acp.shutdown_wait",
            ),
            errors,
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (
                self._prompt_task,
                self._watcher,
                self._stderr_reader,
            )
            if task is not None and task is not current
        )
        for task in tasks:
            task.cancel()
        await collect_cleanup_errors(
            asyncio.gather(*tasks, return_exceptions=True),
            errors,
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
        self._prompt_task = None
        self._watcher = None
        self._stderr_reader = None
        authority, self._driver_authority = self._driver_authority, None
        if authority is not None:
            authority.close()
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        await collect_cleanup_errors(
            self._events.put(None), errors, timeout=CLEANUP_TIMEOUT_SECONDS
        )
        raise_collected_errors("ACP driver close failed", errors)

    async def _watch_process(self, proc: Any) -> None:
        returncode = await proc.wait()
        if not self._closed:
            await self._emit(
                HarnessEventType.RUNTIME_EXITED,
                turn=self._active if self._active.value else None,
                data={"returncode": returncode},
            )

    async def _observe_stream(self, event: StreamEvent) -> None:
        if event.direction is not StreamDirection.OUTGOING:
            return
        if event.message.get("method") != "session/prompt":
            return
        future = self._prompt_sent
        if future is not None and not future.done():
            future.set_result(None)

    async def _session_update(self, session_id: str, update: Any) -> None:
        if session_id != self._native_session_id:
            await self._emit(
                HarnessEventType.RUNTIME_WARNING,
                data={"code": "foreign_session_update"},
            )
            return
        turn = self._active if self._active.value else None
        if isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if not isinstance(text, str):
                return
            block_id = update.message_id or self._fallback_block_id
            self._output_blocks.add(block_id)
            await self._emit(
                HarnessEventType.ASSISTANT_DELTA,
                turn=turn,
                data={"block_id": block_id, "delta": text},
                native_payload=update,
            )
            return
        if isinstance(update, ToolCallStart):
            await self._emit(
                HarnessEventType.TOOL_STARTED,
                turn=turn,
                data={
                    "tool_call_ref": update.tool_call_id,
                    "label": update.title,
                },
                native_payload=update,
            )
            return
        if isinstance(update, ToolCallProgress):
            status = str(update.status or "in_progress")
            if status in {"completed", "failed"}:
                type_ = HarnessEventType.TOOL_COMPLETED
                data = {
                    "tool_call_ref": update.tool_call_id,
                    "label": update.title or "",
                    "outcome": "succeeded" if status == "completed" else "failed",
                }
            else:
                type_ = HarnessEventType.TOOL_UPDATED
                data = {
                    "tool_call_ref": update.tool_call_id,
                    "label": update.title or "",
                    "state": status,
                }
            await self._emit(type_, turn=turn, data=data, native_payload=update)
            return
        if isinstance(update, UsageUpdate):
            await self._emit(
                HarnessEventType.CONTEXT_UPDATED,
                turn=turn,
                data={
                    "context_tokens": max(0, update.used),
                    "context_window": max(0, update.size),
                },
                native_payload=update,
            )
            return
        await self._emit(
            HarnessEventType.SESSION_UPDATED,
            turn=turn,
            data={"record_type": getattr(update, "session_update", "unknown")},
            native_payload=update,
        )

    async def _request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: Any,
    ) -> RequestPermissionResponse:
        if session_id != self._native_session_id or not self._active.value:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        ref = PermissionRef(f"permission_{uuid.uuid4().hex}")
        future = asyncio.get_running_loop().create_future()
        self._permissions[ref] = (future, options)
        await self._emit(
            HarnessEventType.PERMISSION_REQUESTED,
            turn=self._active,
            data={
                "permission_ref": str(ref),
                "tool_call_ref": str(getattr(tool_call, "tool_call_id", "")),
                "label": str(getattr(tool_call, "title", "")),
                "options": tuple(
                    {
                        "id": option.option_id,
                        "name": option.name,
                        "kind": option.kind,
                    }
                    for option in options
                ),
            },
            native_payload=tool_call,
        )
        decision = await future
        self._permissions.pop(ref, None)
        if decision is PermissionDecision.DENY:
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled")
            )
        selected = _preferred_permission(options)
        if selected is None:
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled")
            )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id=selected.option_id,
            )
        )

    async def _unsupported_extension(self, method: str) -> None:
        await self._emit(
            HarnessEventType.RUNTIME_WARNING,
            turn=self._active if self._active.value else None,
            data={"code": "unsupported_extension", "method": f"_{method}"},
        )

    async def _emit(
        self,
        type_: HarnessEventType,
        *,
        turn: TurnRef | None = None,
        data: dict[str, Any] | None = None,
        native_payload: Any = None,
    ) -> None:
        await self._events.put(
            self._event(
                type_,
                turn=turn,
                data=data,
                native_payload=native_payload,
            )
        )

    def _event(
        self,
        type_: HarnessEventType,
        *,
        turn: TurnRef | None = None,
        data: dict[str, Any] | None = None,
        native_payload: Any = None,
    ) -> HarnessEvent:
        return HarnessEvent.normalized(
            type=type_,
            driver="acp",
            session_ref=self._session_ref,
            turn_ref=turn,
            native_session_id=self._native_session_id,
            native_turn_id=self._active_native_turn_id,
            data=data or {},
            native_payload=native_payload,
        )

    def _require_active(self, turn: TurnRef) -> None:
        if turn != self._active or not self._active.value:
            raise RuntimeError("stale or foreign active turn")


def _preferred_permission(
    options: list[PermissionOption],
) -> PermissionOption | None:
    for kind in ("allow_once", "allow_always"):
        for option in options:
            if option.kind == kind:
                return option
    return options[0] if options else None


GenericAcpDriver = AcpDriver


# Executable basename -> verified model flag. Keep this private and small:
# unverified flags must never enter the table, because a guessed flag can be
# ignored while Puffo incorrectly reports that the requested model is active.
_MODEL_FLAG_BY_EXECUTABLE = {
    "gemini": "-m",  # gemini-cli 0.57.0, verified
    "kimi": "-m",  # kimi-cli 1.49.0, verified
}

_MODEL_FLAG_SPELLINGS = ("-m", "--model")


def _basename(executable: str) -> str:
    name = Path(executable).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def operator_pinned_model(launch_args: Sequence[str]) -> bool:
    """True when launch_args already carry a model flag (any spelling)."""
    return any(
        arg in _MODEL_FLAG_SPELLINGS or arg.startswith("--model=")
        for arg in launch_args
    )


def model_launch_args(executable: str, model: str) -> tuple[str, ...]:
    """Return verified model-selection launch arguments for an ACP target."""
    if not model:
        return ()
    flag = _MODEL_FLAG_BY_EXECUTABLE.get(_basename(executable))
    if flag is None:
        return ()
    return (flag, model)


def _to_acp_stdio_server(spec: McpServerSpec) -> McpServerStdio:
    """Convert Puffo's provider-neutral MCP spec to the ACP wire shape.

    The two shapes correspond field for field; only the container types
    differ (tuple/mapping here, list/``EnvVariable`` rows on the wire).
    """

    return McpServerStdio(
        name=spec.name,
        command=spec.command,
        args=list(spec.args),
        env=[
            EnvVariable(name=key, value=value)
            for key, value in spec.environment.items()
        ],
    )


_LINGTAI_CONSTRAINED_PROFILES = frozenset({"puffo-v0", "puffo-v1"})


def _lingtai_constrained_profile(command: tuple[str, ...]) -> str:
    """The constrained LingTai profile argv selects, or "" for none.

    Independent of argv[0]; both ``--profile X`` and ``--profile=X``
    spellings after the ``acp`` token are recognised.
    """

    try:
        acp_index = command.index("acp")
    except ValueError:
        return ""
    profile_args = command[acp_index + 1 :]
    for index, arg in enumerate(profile_args):
        if arg == "--profile" and index + 1 < len(profile_args):
            candidate = profile_args[index + 1]
        elif arg.startswith("--profile="):
            candidate = arg.removeprefix("--profile=")
        else:
            continue
        if candidate in _LINGTAI_CONSTRAINED_PROFILES:
            return candidate
    return ""


def _uses_lingtai_driver_authority(command: tuple[str, ...]) -> bool:
    """True for every constrained LingTai profile, independent of argv[0].

    Both ``puffo-v0`` and ``puffo-v1`` refuse to start without a
    successful Driver authority handshake (LingTai #1624), so both get
    the authority FD and the guarded POSIX spawn path. This is a wider
    predicate than ``selects_puffo_v0_profile``, which only controls
    the empty MCP projection.
    """

    return bool(_lingtai_constrained_profile(command))


def selects_puffo_v0_profile(command: tuple[str, ...]) -> bool:
    """True when argv selects LingTai's ``puffo-v0`` profile.

    v0 rejects a non-empty ``mcpServers`` at ``session/new``, so only
    this profile keeps the MCP projection empty; ``puffo-v1`` receives
    Puffo's server while still using the Driver authority spawn path.
    """

    return _lingtai_constrained_profile(command) == "puffo-v0"
