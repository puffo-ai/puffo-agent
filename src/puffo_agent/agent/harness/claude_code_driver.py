"""Claude Code CLI Driver with non-blocking stream-json turn delivery."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from ..cli_bin import normalize_launch_argv
from .driver import (
    CancelCapability,
    CompactCapability,
    CompactReceipt,
    CompactRequest,
    ContextStatus,
    ContextStatusCapability,
    DriverCapabilities,
    Driver,
    HarnessEvent,
    HarnessEventType,
    PermissionDecision,
    PermissionRef,
    ProtocolDiagnostics,
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
from .subprocess_io import drain_subprocess_stream


class _ContextUsageUnsupported(RuntimeError):
    pass


_CONTEXT_USAGE_TIMEOUT_SECONDS = 2.0


def claude_capabilities(compact_advertised: bool = False) -> DriverCapabilities:
    return DriverCapabilities(
        session_resume=True,
        inflight_turn_recovery=False,
        steer=SteerCapability.NONE,
        cancel=CancelCapability.NONE,
        context_status=ContextStatusCapability.PULL,
        compact=(
            CompactCapability.SESSION_COMMAND
            if compact_advertised
            else CompactCapability.NONE
        ),
        permission_bridge=False,
    )


class ClaudeCodeCliDriver(Driver):
    # A second stream-json user frame is queued as another Claude native Turn.
    # It is not an admission into the active Puffo Turn, even when written just
    # after a tool result, so the scheduler must deliver it through next-turn.
    static_steer_capability = SteerCapability.NONE

    def __init__(
        self,
        process_factory: Callable[..., Any] | None = None,
        *,
        executable_version: str = "",
        replay_timeout: float = 30,
    ):
        self.process_factory = process_factory
        self.executable_version = executable_version
        self.replay_timeout = replay_timeout
        self._proc: Any = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._init: asyncio.Future[dict[str, Any]] | None = None
        self._pending_replay: asyncio.Future[str] | None = None
        self._pending_content = ""
        self._pending_uuid = ""
        self._runtime_ref = RuntimeRef(f"runtime_{uuid.uuid4().hex}")
        self._session_ref = SessionRef("")
        self._native_session_id = ""
        self._resumed = False
        self._init_commands: tuple[str, ...] = ()
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._closed = False
        self._compact_advertised = False
        self._output_block_counter = 0
        self._tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self._context_request_counter = 0
        self._context_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._context_usage_supported: bool | None = None
        self._context = ContextStatus(stale=True)

    def current_capabilities(self) -> DriverCapabilities:
        return claude_capabilities(self._compact_advertised)

    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened:
        if self._proc is not None:
            raise RuntimeError("driver is already open")
        if self._closed:
            self._prepare_reopen()
        launch_args = list(spec.launch_args)
        if spec.auto_compact_threshold_tokens is not None:
            launch_args = _replace_option_value(
                launch_args,
                "--autocompact",
                str(spec.auto_compact_threshold_tokens),
            )
            # The CLI can receive /compact as its first stream-json user
            # frame. Treat the configured native threshold as the startup
            # capability so context admission can compact before system/init.
            self._compact_advertised = True
        args = [
            *normalize_launch_argv(spec.executable or "claude"),
            *launch_args,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--replay-user-messages",
            "--verbose",
        ]
        if spec.model:
            args.extend(["--model", spec.model])
        self._resumed = resume is not None
        native = str(resume) if resume is not None else str(uuid.uuid4())
        self._native_session_id = native
        self._session_ref = SessionRef(native)
        if resume is not None:
            args.extend(["--resume", native])
        else:
            # Claude Code does not emit system/init until it receives the
            # first stream-json user frame.  Supplying the ID lets open()
            # establish the durable session without inventing a probe turn.
            args.extend(["--session-id", native])
        if self.process_factory is None:
            env = os.environ.copy()
            env.update(spec.environment)
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.workspace_dir or None,
                env=env,
                limit=16 * 1024 * 1024,
            )
        else:
            try:
                self._proc = self.process_factory(args, spec)
            except TypeError:
                self._proc = self.process_factory(args)
            if asyncio.iscoroutine(self._proc):
                self._proc = await self._proc
        self._init = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_reader = asyncio.create_task(
            drain_subprocess_stream(getattr(self._proc, "stderr", None))
        )
        # Older CLI builds and test doubles may emit init eagerly.  Give the
        # reader one scheduling opportunity while keeping current CLI startup
        # non-blocking until the first real turn arrives.
        await asyncio.sleep(0)
        return RuntimeOpened(
            self._runtime_ref,
            self._session_ref,
            self._native_session_id,
            self._resumed,
            claude_capabilities(self._compact_advertised),
            ProtocolDiagnostics(
                executable_version=self.executable_version,
                schema_source="documented",
                native_capabilities=self._init_commands,
            ),
        )

    async def start_turn(self, input: TurnInput):
        if self._active.value or self._pending_replay is not None:
            raise RuntimeError("one turn is already active")
        local = TurnRef(f"turn_{uuid.uuid4().hex}")
        replay_uuid = input.client_correlation_id or str(uuid.uuid4())
        self._active = local
        self._pending_content = _normalize_content(input.content)
        self._pending_uuid = replay_uuid
        self._active_native_turn_id = replay_uuid
        self._pending_replay = asyncio.get_running_loop().create_future()
        frame = {
            "type": "user",
            "session_id": self._native_session_id,
            "parent_tool_use_id": None,
            "uuid": replay_uuid,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": input.content}],
            },
        }
        try:
            # The daemon has already opened its durable local turn. A
            # successful stdin drain is therefore enough to return control;
            # Claude's replay remains useful telemetry but must not gate MCP
            # calls made while the provider is processing this frame.
            await self._write(frame)
        except BaseException:
            self._active = TurnRef("")
            self._active_native_turn_id = ""
            self._clear_pending_replay()
            raise
        return TurnStarted(
            local,
            native_turn_id=replay_uuid,
            accepted=True,
            delivery="stdin_written",
        )

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn: TurnRef):
        return UnsupportedCapability("cancel")

    async def context_status(self):
        if self._context_usage_supported is False:
            return UnsupportedCapability("context_status")
        if self._proc is None:
            return ContextStatus(
                used_tokens=self._context.used_tokens,
                context_window=self._context.context_window,
                stale=True,
                measured_at=self._context.measured_at,
                auto_compact_threshold_tokens=(
                    self._context.auto_compact_threshold_tokens
                ),
                auto_compact_enabled=self._context.auto_compact_enabled,
            )
        self._context_request_counter += 1
        request_id = f"ctx_{self._context_request_counter}"
        future = asyncio.get_running_loop().create_future()
        self._context_requests[request_id] = future
        try:
            await self._write({
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "get_context_usage"},
            })
            usage = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=_CONTEXT_USAGE_TIMEOUT_SECONDS,
            )
        except _ContextUsageUnsupported:
            self._context_usage_supported = False
            return UnsupportedCapability("context_status")
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            return ContextStatus(
                used_tokens=self._context.used_tokens,
                context_window=self._context.context_window,
                stale=True,
                measured_at=self._context.measured_at,
                auto_compact_threshold_tokens=(
                    self._context.auto_compact_threshold_tokens
                ),
                auto_compact_enabled=self._context.auto_compact_enabled,
            )
        finally:
            pending = self._context_requests.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
        self._context_usage_supported = True
        auto_compact_threshold = _positive_int(
            usage.get("autoCompactThreshold")
        )
        raw_auto_compact_enabled = usage.get("isAutoCompactEnabled")
        auto_compact_enabled = (
            raw_auto_compact_enabled
            if isinstance(raw_auto_compact_enabled, bool)
            else None
        )
        if auto_compact_enabled is True and auto_compact_threshold is not None:
            # Claude exposes this control response before system/init, which
            # lets context admission discover /compact for the first turn.
            self._compact_advertised = True
        self._context = ContextStatus(
            used_tokens=_positive_int(usage.get("totalTokens")),
            context_window=_positive_int(usage.get("rawMaxTokens")),
            stale=False,
            measured_at=datetime.now(timezone.utc).isoformat(),
            auto_compact_threshold_tokens=auto_compact_threshold,
            auto_compact_enabled=auto_compact_enabled,
        )
        return self._context

    async def compact(self, request: CompactRequest):
        if not self._compact_advertised:
            return UnsupportedCapability("compact")
        if self._active.value:
            raise RuntimeError("Claude /compact requires an idle session")
        text = "/compact"
        if request.instructions:
            text += " " + request.instructions
        frame = {
            "type": "user",
            "session_id": self._native_session_id,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": text},
        }
        await self._write(frame)
        return CompactReceipt(True)

    async def resolve_permission(
        self, request: PermissionRef, decision: PermissionDecision
    ):
        return UnsupportedCapability("permission_bridge")

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
        proc, self._proc = self._proc, None
        if proc is not None and getattr(proc, "returncode", None) is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._stderr_reader is not None:
            self._stderr_reader.cancel()
            await asyncio.gather(self._stderr_reader, return_exceptions=True)
        self._reader = None
        self._stderr_reader = None
        self._fail_pending_futures("Claude Code CLI closed")
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._pending_content = ""
        self._pending_uuid = ""
        self._tool_calls.clear()
        await self._events.put(None)

    def _prepare_reopen(self) -> None:
        """Discard transient state tied to the process that was closed."""
        self._closed = False
        self._events = asyncio.Queue()
        self._init = None
        self._pending_replay = None
        self._pending_content = ""
        self._pending_uuid = ""
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._tool_calls.clear()
        self._compact_advertised = False
        self._resumed = False
        self._init_commands = ()
        self._context_usage_supported = None
        self._context = ContextStatus(
            used_tokens=self._context.used_tokens,
            context_window=self._context.context_window,
            stale=True,
            measured_at=self._context.measured_at,
            auto_compact_threshold_tokens=(
                self._context.auto_compact_threshold_tokens
            ),
            auto_compact_enabled=self._context.auto_compact_enabled,
        )

    def _fail_pending_futures(self, message: str) -> None:
        for future in (
            self._init,
            *self._context_requests.values(),
        ):
            if future is not None and not future.done():
                future.set_exception(RuntimeError(message))
        self._init = None
        self._clear_pending_replay()
        self._context_requests.clear()

    def _clear_pending_replay(self) -> None:
        pending = self._pending_replay
        if pending is not None and not pending.done():
            pending.cancel()
        self._pending_replay = None
        self._pending_content = ""
        self._pending_uuid = ""

    async def _write(self, frame: dict[str, Any]) -> None:
        encoded = (
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )
        async with self._write_lock:
            self._proc.stdin.write(encoded)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._emit(
                        HarnessEventType.RUNTIME_WARNING,
                        data={"code": "protocol_parse"},
                    )
                    continue
                await self._handle(frame)
        finally:
            self._clear_pending_replay()
            if self._init is not None and not self._init.done():
                self._init.set_exception(RuntimeError("Claude exited before init"))
            if not self._closed:
                await self._emit(
                    HarnessEventType.RUNTIME_EXITED,
                    turn_ref=self._active if self._active.value else None,
                )

    async def _handle(self, frame: dict[str, Any]) -> None:
        type_ = str(frame.get("type") or "")
        subtype = str(frame.get("subtype") or "")
        if type_ == "control_response":
            self._handle_control_response(frame)
            return
        if type_ == "system" and subtype == "init":
            await self._complete_init(frame)
            return
        if self._is_exact_replay(frame):
            await self._complete_replay(frame)
            return
        if type_ == "assistant":
            await self._handle_assistant(frame)
            return
        if type_ == "user" and self._active.value:
            await self._handle_tool_results(frame)
            return
        if type_ == "result":
            await self._handle_result(frame, subtype)
            return
        if type_ == "system" and (
            (subtype == "status" and str(frame.get("status") or "") == "compacting")
            or subtype == "compact_boundary"
        ):
            # `/compact` acceptance is not completion: the boundary frame is
            # the only terminal confirmation the CLI emits.
            await self._emit(
                HarnessEventType.COMPACTION_STARTED
                if subtype == "status"
                else HarnessEventType.COMPACTION_COMPLETED,
                turn_ref=self._active if self._active.value else None,
                native_payload=frame,
            )
            return
        await self._emit(
            HarnessEventType.SESSION_UPDATED,
            data={"record_type": type_ or "unknown"},
            native_payload=frame,
        )

    def _handle_control_response(self, frame: dict[str, Any]) -> None:
        response = frame.get("response")
        if not isinstance(response, dict):
            return
        request_id = str(response.get("request_id") or "")
        future = self._context_requests.get(request_id)
        if future is None or future.done():
            return
        if response.get("subtype") == "error":
            future.set_exception(
                _ContextUsageUnsupported(
                    str(response.get("error") or "context query failed")
                )
            )
            return
        usage = response.get("response")
        if not isinstance(usage, dict):
            future.set_exception(RuntimeError("invalid context usage response"))
            return
        future.set_result(usage)

    async def _complete_init(self, frame: dict[str, Any]) -> None:
        if self._init is None or self._init.done():
            return
        native = str(frame.get("session_id") or "")
        if not native:
            self._init.set_exception(
                RuntimeError("Claude system/init omitted session_id")
            )
            return
        self._native_session_id = native
        self._session_ref = SessionRef(native)
        commands = frame.get("slash_commands") or ()
        self._init_commands = tuple(str(value) for value in commands)
        self._compact_advertised = self._compact_advertised or (
            "/compact" in self._init_commands or "compact" in self._init_commands
        )
        self._init.set_result(frame)
        await self._emit(
            HarnessEventType.SESSION_RESUMED
            if self._resumed
            else HarnessEventType.SESSION_OPENED,
            native_payload=frame,
        )

    async def _complete_replay(self, frame: dict[str, Any]) -> None:
        pending = self._pending_replay
        if pending is None:
            return
        if not pending.done():
            pending.set_result(str(frame.get("uuid")))
        self._clear_pending_replay()
        await self._emit(
            HarnessEventType.TURN_STARTED, turn_ref=self._active, native_payload=frame
        )

    async def _handle_assistant(self, frame: dict[str, Any]) -> None:
        if not self._active.value:
            await self._emit(
                HarnessEventType.SESSION_UPDATED,
                data={"record_type": "assistant"},
                native_payload=frame,
            )
            return
        for index, block in enumerate(
            (frame.get("message") or {}).get("content") or ()
        ):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                await self._emit_assistant_text(frame, block, index)
            elif block.get("type") == "tool_use":
                await self._emit_tool_started(frame, block)

    async def _emit_assistant_text(
        self, frame: dict[str, Any], block: dict[str, Any], index: int
    ) -> None:
        self._output_block_counter += 1
        # Block identity must be per content block, not per frame: Anthropic
        # `text` blocks carry no `id`, so falling back to the frame `uuid`
        # alone gives every text block in a multi-block frame the same public
        # id.  The projector then suppresses the second block's `start` and the
        # validator rejects the resulting `end -> delta` transition, aborting
        # the whole turn as `runtime_event_processing_failed`.
        frame_uuid = frame.get("uuid")
        block_id = str(
            block.get("id")
            or (
                f"{frame_uuid}:{index}"
                if frame_uuid
                else f"result_{self._output_block_counter}"
            )
        )
        await self._emit(
            HarnessEventType.ASSISTANT_DELTA,
            turn_ref=self._active,
            data={"text": str(block.get("text") or ""), "block_id": block_id},
            native_payload=frame,
        )
        await self._emit(
            HarnessEventType.ASSISTANT_COMPLETED,
            turn_ref=self._active,
            data={"block_id": block_id},
            native_payload=frame,
        )

    async def _emit_tool_started(
        self, frame: dict[str, Any], block: dict[str, Any]
    ) -> None:
        tool_call_id = str(block.get("id") or "")
        arguments = block.get("input") or {}
        self._tool_calls[tool_call_id] = (
            str(block.get("name") or ""),
            arguments if isinstance(arguments, dict) else {},
        )
        await self._emit(
            HarnessEventType.TOOL_STARTED,
            turn_ref=self._active,
            data={
                "tool_call_ref": tool_call_id,
                "label": str(block.get("name") or "Tool"),
            },
            native_payload=frame,
        )

    async def _handle_tool_results(self, frame: dict[str, Any]) -> None:
        for block in (frame.get("message") or {}).get("content") or ():
            if isinstance(block, dict) and block.get("type") == "tool_result":
                await self._emit_tool_result(frame, block)

    async def _emit_tool_result(
        self, frame: dict[str, Any], block: dict[str, Any]
    ) -> None:
        tool_call_id = str(block.get("tool_use_id") or "")
        tool_name, arguments = self._tool_calls.pop(tool_call_id, ("", {}))
        await self._emit(
            HarnessEventType.TOOL_COMPLETED,
            turn_ref=self._active,
            data={
                "tool_call_ref": tool_call_id,
                "label": tool_name or "Tool",
                "outcome": "failed" if block.get("is_error") else "succeeded",
            },
            native_payload={
                "_puffo_internal": "tool_result",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "result": block.get("content"),
                "is_error": bool(block.get("is_error")),
            },
        )

    async def _handle_result(self, frame: dict[str, Any], subtype: str) -> None:
        if not self._active.value:
            await self._emit(
                HarnessEventType.SESSION_UPDATED,
                data={"record_type": "result"},
                native_payload=frame,
            )
            return
        usage = frame.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0) + int(
            usage.get("cache_creation_input_tokens") or 0
        )
        context_tokens = input_tokens + int(
            usage.get("cache_read_input_tokens") or 0
        )
        outcome = "failed" if subtype not in {"success", ""} else "succeeded"
        data: dict[str, Any] = {
            "outcome": outcome,
            "input_tokens": input_tokens,
            "output_tokens": int(usage.get("output_tokens") or 0),
            "context_tokens": context_tokens,
        }
        if outcome == "failed":
            data["error_code"] = _result_error_code(frame, subtype)
        await self._emit(
            HarnessEventType.TURN_COMPLETED,
            turn_ref=self._active,
            data=data,
            native_payload=frame,
        )
        self._clear_pending_replay()
        self._active, self._active_native_turn_id = TurnRef(""), ""
        # A `tool_use` whose `tool_result` never arrives would otherwise keep
        # its name and arguments alive until close, so a later turn reusing the
        # id would report the stale call.
        self._tool_calls.clear()

    def _is_exact_replay(self, frame: dict[str, Any]) -> bool:
        if self._pending_replay is None or self._pending_replay.done():
            return False
        if frame.get("type") != "user" or frame.get("isReplay") is not True:
            return False
        if str(frame.get("session_id") or "") != self._native_session_id:
            return False
        if frame.get("parent_tool_use_id") is not None:
            return False
        if str(frame.get("uuid") or "") != self._pending_uuid:
            return False
        return (
            _normalize_content((frame.get("message") or {}).get("content"))
            == self._pending_content
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
                driver="claude-code",
                session_ref=self._session_ref,
                turn_ref=turn_ref,
                native_session_id=self._native_session_id,
                native_turn_id=self._active_native_turn_id,
                data=data or {},
                native_payload=native_payload,
            )
        )


def _normalize_content(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        ).replace("\r\n", "\n")
    return ""


def _replace_option_value(args: list[str], option: str, value: str) -> list[str]:
    """Return argv with exactly one current value for ``option``."""
    normalized: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == option:
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if argument.startswith(f"{option}="):
            index += 1
            continue
        normalized.append(argument)
        index += 1
    normalized.extend((option, value))
    return normalized


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _result_error_code(frame: dict[str, Any], subtype: str) -> str:
    errors = frame.get("errors")
    if isinstance(errors, list) and any(
        "No conversation found with session ID:" in value
        for value in errors
        if isinstance(value, str)
    ):
        return "invalid_resume"
    normalized = subtype.strip().lower()
    return normalized or "execution_error"


ClaudeDriver = ClaudeCodeCliDriver
