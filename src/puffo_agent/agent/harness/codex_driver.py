"""Codex app-server Driver using the exercised stable v2 JSONL shapes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from ..._proc import no_window_kwargs
from ..errors import AgentAPIError, ProviderFailureError
from ..provider_failures import (
    classify_provider_failure,
    provider_failure,
    provider_failure_message,
)
from .driver import (
    CancelCapability,
    CancelReceipt,
    CompactCapability,
    CompactReceipt,
    CompactRequest,
    ContextStatus,
    ContextStatusCapability,
    DriverCapabilities,
    Driver,
    HarnessEvent,
    HarnessEventType,
    InputReceipt,
    PermissionDecision,
    PermissionReceipt,
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
)
from .subprocess_io import drain_subprocess_stream

CODEX_CAPABILITIES = DriverCapabilities(
    session_resume=True,
    inflight_turn_recovery=False,
    steer=SteerCapability.CURRENT_TURN,
    cancel=CancelCapability.TYPED,
    context_status=ContextStatusCapability.PUSH,
    compact=CompactCapability.TYPED,
    permission_bridge=True,
)

logger = logging.getLogger(__name__)


_PERMISSION_REQUEST_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "execCommandApproval",
    "applyPatchApproval",
})


_SENSITIVE_ERROR_FIELD = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization)[\"']?\s*[:=]\s*)"
    r"(?P<value>(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+))"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)")
_TOKENISH = re.compile(
    r"(?i)\b(?:sk[_-][a-z0-9_-]{12,}|eyJ[a-zA-Z0-9_-]{12,}"
    r"\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?)"
)
_TOOL_ITEM_TYPES = frozenset(
    {"mcpToolCall", "dynamicToolCall", "functionCall", "toolCall"}
)
_COMPACTION_ITEM_TYPE = "contextCompaction"


def _tool_label(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("tool") or "")
    return (
        name.rsplit("__", 1)[-1]
        if name.startswith("mcp__") and "__" in name
        else name
    )


def _safe_jsonrpc_error_message(message: Any) -> str:
    """Keep a bounded diagnostic while never copying credential-shaped text."""
    if not isinstance(message, str):
        return "(missing or invalid provider message)"
    compact = " ".join(message.split())
    redacted = _SENSITIVE_ERROR_FIELD.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", compact
    )
    redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
    redacted = _TOKENISH.sub("[REDACTED]", redacted)
    return redacted[:300] or "(empty provider message)"


def _safe_jsonrpc_error_code(code: Any) -> str:
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        return "unknown"
    return _safe_jsonrpc_error_message(str(code))[:64]


def _jsonrpc_error_context(error: Any) -> str:
    """Return only classification fields; this value is never surfaced."""
    if not isinstance(error, dict):
        return str(error or "")[:2048]
    values: list[str] = []
    for key in ("code", "message", "type", "status"):
        value = error.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            values.append(str(value))
    data = error.get("data")
    if isinstance(data, dict):
        for key in ("code", "message", "type", "status", "error"):
            value = data.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                values.append(str(value))
    return " ".join(values)[:2048]


def _jsonrpc_error_status(error: Any) -> int | None:
    if not isinstance(error, dict):
        return None
    candidates = [error.get("status"), error.get("code")]
    data = error.get("data")
    if isinstance(data, dict):
        candidates.extend((data.get("status"), data.get("code")))
    for candidate in candidates:
        if type(candidate) is int and 400 <= candidate <= 599:
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            parsed = int(candidate)
            if 400 <= parsed <= 599:
                return parsed
    return None


_INVALID_RESUME_MARKERS = (
    "no rollout found",
    "rollout not found",
    "thread not found",
)


def _is_invalid_resume_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _INVALID_RESUME_MARKERS)


def _classify_jsonrpc_error(error: Any) -> Exception:
    """Translate a Codex JSON-RPC error into Global Inbox recovery semantics."""
    error_obj = error if isinstance(error, dict) else {}
    code = _safe_jsonrpc_error_code(error_obj.get("code"))
    message = _safe_jsonrpc_error_message(error_obj.get("message", error))
    detail = f"Codex JSON-RPC error code={code}: {message}"
    context = _jsonrpc_error_context(error)
    if _is_invalid_resume_error(message):
        # Missing rollout: keep detail for the fresh-session fallback.
        logger.warning(
            "codex provider request failed (invalid_resume): %s", detail
        )
        return AgentAPIError(detail, error_code="invalid_resume")
    error_code = classify_provider_failure(
        status=_jsonrpc_error_status(error),
        diagnostic=context,
    )
    failure = provider_failure(error_code)
    logger.warning("codex provider request failed (%s): %s", error_code, detail)
    public_message = provider_failure_message(error_code)
    if failure.is_auth or failure.retryable:
        return AgentAPIError(
            public_message,
            is_auth=failure.is_auth,
            error_code=error_code,
        )
    return ProviderFailureError(public_message, error_code=error_code)


def _permission_response(
    method: str,
    params: dict[str, Any],
    decision: PermissionDecision,
) -> dict[str, Any]:
    """Encode one operator decision in the native Codex response schema."""
    approved = decision is PermissionDecision.APPROVE
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "accept" if approved else "decline"}
    if method == "item/permissions/requestApproval":
        if not approved:
            return {"permissions": {}}
        requested = params.get("permissions")
        return {
            "permissions": requested if isinstance(requested, dict) else {},
            "scope": "session",
        }
    if method == "mcpServer/elicitation/request":
        return {
            "action": "accept" if approved else "decline",
            "content": {} if approved else None,
        }
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {
            "decision": (
                "approved"
                if approved
                else {"denied": {"rejection": "Denied by operator"}}
            )
        }
    raise RuntimeError(f"unsupported permission request method: {method}")


class CodexAppServerDriver(Driver):
    static_steer_capability = SteerCapability.CURRENT_TURN

    def __init__(
        self,
        process_factory: Callable[[RuntimeSpec], Any] | None = None,
        *,
        executable_version: str = "",
        request_timeout_seconds: float = 60.0,
    ):
        self.process_factory = process_factory
        self.executable_version = executable_version
        self.request_timeout_seconds = request_timeout_seconds
        self._proc: Any = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._request_id = 0
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._runtime_ref = RuntimeRef(f"runtime_{uuid.uuid4().hex}")
        self._session_ref = SessionRef("")
        self._native_session_id = ""
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._context = ContextStatus(stale=True)
        self._permission_requests: dict[
            PermissionRef, tuple[int | str, str, dict[str, Any]]
        ] = {}
        self._open_output_blocks: set[str] = set()
        # Synthetic id for the currently open id-less agent message, plus the
        # counter that keeps successive ones distinct.
        self._fallback_block_id: str | None = None
        self._fallback_block_counter = 0
        self._closed = False
        # Turn-scoped token accounting: the baseline freezes on the first
        # observed thread totals and the latest derived values stand at
        # turn/completed. Cleared on every terminal, start-failure, and close.
        self._usage_baseline: tuple[int, int] | None = None
        self._usage_latest: dict[str, int] = {}

    def current_capabilities(self) -> DriverCapabilities:
        return CODEX_CAPABILITIES

    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened:
        if self._proc is not None:
            raise RuntimeError("driver is already open")
        if self._closed:
            self._prepare_reopen()
        await self._start_process(spec)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_reader = asyncio.create_task(
            drain_subprocess_stream(getattr(self._proc, "stderr", None))
        )
        await self._initialize_app_server()
        result, resumed = await self._open_thread(spec, resume)
        thread = result.get("thread", result) if isinstance(result, dict) else {}
        native = str(thread.get("id") or thread.get("threadId") or str(resume or ""))
        if not native:
            raise RuntimeError("Codex thread response omitted native thread id")
        self._native_session_id = native
        self._session_ref = SessionRef(native)
        await self._emit(
            HarnessEventType.SESSION_RESUMED
            if resumed
            else HarnessEventType.SESSION_OPENED,
            native_payload=result,
        )
        return RuntimeOpened(
            self._runtime_ref,
            self._session_ref,
            native,
            resumed,
            CODEX_CAPABILITIES,
            ProtocolDiagnostics(
                executable_version=self.executable_version,
                schema_source="generated",
                native_capabilities=(
                    "turn/steer",
                    "turn/interrupt",
                    "thread/compact/start",
                    "permission_bridge",
                ),
            ),
        )

    async def _start_process(self, spec: RuntimeSpec) -> None:
        if self.process_factory is None:
            executable = spec.executable or "codex"
            # Merge, matching ClaudeCodeCliDriver.open: `RuntimeSpec.environment`
            # is a Mapping that invites a delta, and replacing the child's whole
            # environment would strip PATH/HOME from `codex app-server`.
            env = os.environ.copy()
            env.update(spec.environment)
            self._proc = await asyncio.create_subprocess_exec(
                executable,
                *spec.launch_args,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.workspace_dir or None,
                env=env,
                # One provider frame can carry a large tool result; the default
                # 64 KiB stream limit would terminate the reader mid-session.
                limit=16 * 1024 * 1024,
                **no_window_kwargs(),
            )
        else:
            self._proc = self.process_factory(spec)
            if asyncio.iscoroutine(self._proc):
                self._proc = await self._proc

    async def _initialize_app_server(self) -> None:
        await self._request(
            "initialize",
            {
                "clientInfo": {"name": "puffo-agent", "version": "1"},
                "capabilities": {},
            },
        )
        await self._write({"method": "initialized", "params": {}})

    async def _open_thread(
        self, spec: RuntimeSpec, resume: SessionRef | None
    ) -> tuple[Any, bool]:
        thread_config = {
            "cwd": spec.workspace_dir,
            "approvalPolicy": (
                "never"
                if spec.permission_mode == "bypassPermissions"
                else "untrusted"
            ),
            "sandbox": spec.sandbox,
            **({"model": spec.model} if spec.model else {}),
        }
        if spec.auto_compact_threshold_tokens is not None:
            thread_config["config"] = {
                "model_auto_compact_token_limit": (
                    spec.auto_compact_threshold_tokens
                )
            }
        if resume is None:
            result = await self._request(
                "thread/start",
                thread_config,
            )
            resumed = False
        else:
            result = await self._request(
                "thread/resume",
                {
                    "threadId": str(resume),
                    **thread_config,
                },
            )
            resumed = True
        return result, resumed

    async def start_turn(self, input: TurnInput):
        if self._active.value:
            raise RuntimeError("one turn is already active")
        # Clear the previous turn before issuing turn/start. The reader may
        # receive this turn's first tokenUsage notification immediately after
        # the response future resolves, before this coroutine resumes.
        self._reset_usage()
        local = TurnRef(f"turn_{uuid.uuid4().hex}")
        self._active = local
        params: dict[str, Any] = {
            "threadId": self._native_session_id,
            "input": [{"type": "text", "text": input.content}],
        }
        if input.client_correlation_id:
            params["clientUserMessageId"] = input.client_correlation_id
        try:
            result = await self._request("turn/start", params)
        except BaseException:
            self._active = TurnRef("")
            self._reset_usage()
            raise
        turn = result.get("turn", result) if isinstance(result, dict) else {}
        native = str(turn.get("id") or turn.get("turnId") or "")
        if not native:
            self._active = TurnRef("")
            self._reset_usage()
            raise RuntimeError("Codex turn/start omitted native turn id")
        self._active_native_turn_id = native
        return TurnStarted(local, native)

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        self._require_active(turn)
        await self._request(
            "turn/steer",
            {
                "threadId": self._native_session_id,
                "expectedTurnId": self._active_native_turn_id,
                "input": [{"type": "text", "text": input.content}],
            },
        )
        return InputReceipt(True, turn, input.client_correlation_id)

    async def cancel_turn(self, turn: TurnRef):
        self._require_active(turn)
        await self._request(
            "turn/interrupt",
            {
                "threadId": self._native_session_id,
                "turnId": self._active_native_turn_id,
            },
        )
        return CancelReceipt(True, turn)

    async def context_status(self):
        return self._context

    async def compact(self, request: CompactRequest):
        if self._active_native_turn_id:
            raise RuntimeError("compaction requires an idle session")
        result = await self._request(
            "thread/compact/start",
            {
                "threadId": self._native_session_id,
            },
        )
        operation = str(result.get("id") if isinstance(result, dict) else "")
        return CompactReceipt(True, operation)

    async def resolve_permission(
        self, request: PermissionRef, decision: PermissionDecision
    ):
        pending = self._permission_requests.get(request)
        if pending is None:
            raise RuntimeError("unknown or stale permission reference")
        request_id, method, params = pending
        await self._write(
            {
                "id": request_id,
                "result": _permission_response(method, params, decision),
            }
        )
        self._permission_requests.pop(request, None)
        await self._emit(
            HarnessEventType.PERMISSION_UPDATED,
            turn_ref=self._active,
            data={
                "permission_ref": str(request),
                "state": decision.value,
            },
        )
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
        self._fail_pending_requests("Codex app-server closed")
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._permission_requests.clear()
        self._open_output_blocks.clear()
        self._fallback_block_id = None
        self._reset_usage()
        self._context = ContextStatus(stale=True)
        await self._events.put(None)

    def _prepare_reopen(self) -> None:
        """Discard transient state tied to the process that was closed."""
        self._closed = False
        self._events = asyncio.Queue()
        self._pending.clear()
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._permission_requests.clear()
        self._open_output_blocks.clear()
        self._fallback_block_id = None
        self._reset_usage()
        self._context = ContextStatus(stale=True)

    def _fail_pending_requests(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(message))
        self._pending.clear()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            # Inside the try: a write that raises must not leak the registered
            # entry, which `_fail_pending_requests` would later resolve with an
            # exception no coroutine is left to retrieve.
            await self._write(
                {"id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, self.request_timeout_seconds)
        except asyncio.TimeoutError:
            # Provider silence must surface as a bounded, retryable failure so
            # Global Inbox recovery re-enqueues instead of waiting forever.
            logger.warning(
                "Codex request %s timed out after %ss",
                method,
                f"{self.request_timeout_seconds:g}",
            )
            raise AgentAPIError(
                f"{provider_failure_message('provider_unavailable')} "
                f"Codex request {method} timed out after "
                f"{self.request_timeout_seconds:g}s.",
                is_auth=False,
                error_code="provider_unavailable",
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def _write(self, frame: dict[str, Any]) -> None:
        encoded = (
            json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
        )
        async with self._write_lock:
            self._proc.stdin.write(encoded)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._proc.stdout.readline()
                except ValueError:
                    # A frame beyond the stream limit leaves stdout partially
                    # consumed; the session cannot be resynchronized, so fall
                    # through to the bounded failure path below.
                    logger.warning(
                        "codex app-server frame exceeded the stdout limit"
                    )
                    break
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
                if not isinstance(frame, dict):
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
                    # One malformed frame must never kill the reader and orphan
                    # every pending request.
                    logger.exception("codex frame dispatch failed")
                    await self._emit(
                        HarnessEventType.RUNTIME_WARNING,
                        data={"code": "frame_dispatch_failed"},
                    )
        finally:
            self._fail_pending_requests("Codex app-server exited")
            if not self._closed:
                await self._emit(HarnessEventType.RUNTIME_EXITED)

    async def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            request_id = frame["id"]
            if not _is_request_id(request_id) or isinstance(request_id, str):
                # We only ever issue integer ids; anything else is unroutable.
                await self._emit(
                    HarnessEventType.RUNTIME_WARNING,
                    data={"code": "protocol_frame"},
                )
                return
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                if "error" in frame:
                    future.set_exception(_classify_jsonrpc_error(frame["error"]))
                else:
                    future.set_result(frame.get("result"))
            return
        if "id" in frame and "method" in frame:
            await self._server_request(frame)
            return
        await self._notification(frame)

    async def _server_request(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if not _is_request_id(request_id):
            await self._emit(
                HarnessEventType.RUNTIME_WARNING,
                data={"code": "protocol_frame"},
            )
            return
        method = str(frame.get("method") or "")
        if method in _PERMISSION_REQUEST_METHODS:
            ref = PermissionRef(f"perm_{uuid.uuid4().hex}")
            params = (
                frame.get("params")
                if isinstance(frame.get("params"), dict)
                else {}
            )
            self._permission_requests[ref] = (request_id, method, params)
            await self._emit(
                HarnessEventType.PERMISSION_REQUESTED,
                turn_ref=self._active,
                data={
                    "permission_ref": str(ref),
                    "state": "pending",
                    "title": "Permission required",
                },
                native_payload=frame,
            )
            return
        await self._write(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "unsupported request"},
            }
        )

    async def _notification(self, frame: dict[str, Any]) -> None:
        method = str(frame.get("method") or "")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        self._log_tool_shape(method, params)
        if method == "turn/started":
            await self._emit(
                HarnessEventType.TURN_STARTED,
                turn_ref=self._active,
                native_payload=frame,
            )
        elif method == "item/agentMessage/delta":
            block_id = self._delta_block_id(params.get("itemId"))
            self._open_output_blocks.add(block_id)
            await self._emit(
                HarnessEventType.ASSISTANT_DELTA,
                turn_ref=self._active,
                data={
                    "text": str(params.get("delta") or ""),
                    "block_id": block_id,
                },
                native_payload=frame,
            )
        elif method == "item/started":
            await self._handle_started_item(frame, params)
        elif method in {"item/completed", "item/agentMessage/completed"}:
            await self._handle_completed_item(frame, params)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", params)
            if not isinstance(usage, dict):
                usage = {}
            self._context = ContextStatus(
                used_tokens=_context_tokens(usage),
                context_window=_integer(usage.get("modelContextWindow")),
                stale=False,
                measured_at=datetime.now(timezone.utc).isoformat(),
            )
            self._update_usage(usage)
            await self._emit(
                HarnessEventType.CONTEXT_UPDATED,
                turn_ref=self._active,
                data={
                    "used_tokens": self._context.used_tokens,
                    "context_window": self._context.context_window,
                },
                native_payload=frame,
            )
        elif method == "turn/completed":
            await self._complete_turn(frame, params)
        else:
            await self._emit(
                HarnessEventType.RUNTIME_WARNING,
                data={"code": "unknown_notification", "method": method},
                native_payload=frame,
            )

    def _delta_block_id(self, item_id: Any) -> str:
        """Public id for an agent-message delta.

        A constant fallback would give two successive id-less messages in one
        turn the same public id, so the second one's `start` is suppressed and
        the validator rejects an `end -> delta` transition.  Reuse the open
        synthetic id while a block is in flight; mint a fresh one after it
        closes.
        """
        if item_id:
            return str(item_id)
        if self._fallback_block_id is None:
            self._fallback_block_counter += 1
            self._fallback_block_id = f"result_{self._fallback_block_counter}"
        return self._fallback_block_id

    def _log_tool_shape(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict) or not (
            "tool" in method.lower()
            or "tool" in str(item.get("type") or "").lower()
            or any(key in item for key in ("tool", "server", "namespace"))
        ):
            return
        logger.debug(
            "codex tool notification shape method=%s type=%s tool=%s server=%s namespace=%s status=%s keys=%s",
            method,
            str(item.get("type") or ""),
            str(item.get("tool") or item.get("name") or ""),
            str(item.get("server") or ""),
            str(item.get("namespace") or ""),
            str(item.get("status") or ""),
            sorted(str(key) for key in item),
        )

    async def _handle_started_item(
        self, frame: dict[str, Any], params: dict[str, Any]
    ) -> None:
        """Normalize the started half of an app-server item.

        Only compaction and tool items carry public meaning; every other
        started type is silently ignored rather than reported as an unknown
        notification, because ``item/started`` itself is a known method.
        """
        item = params.get("item")
        item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        if item_type == _COMPACTION_ITEM_TYPE:
            await self._emit(
                HarnessEventType.COMPACTION_STARTED,
                turn_ref=self._active,
                native_payload=frame,
            )
            return
        if not isinstance(item, dict) or item_type not in _TOOL_ITEM_TYPES:
            return
        await self._emit(
            HarnessEventType.TOOL_STARTED,
            turn_ref=self._active,
            data={
                "tool_call_ref": str(
                    item.get("id") or params.get("itemId") or ""
                ),
                "label": _tool_label(item) or "Tool",
            },
            native_payload=frame,
        )

    async def _handle_completed_item(
        self, frame: dict[str, Any], params: dict[str, Any]
    ) -> None:
        item = params.get("item")
        item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        if item_type == _COMPACTION_ITEM_TYPE:
            # Terminal success for `thread/compact/start`; the JSON-RPC reply
            # to that request is acceptance only.
            await self._emit(
                HarnessEventType.COMPACTION_COMPLETED,
                turn_ref=self._active,
                native_payload=frame,
            )
            return
        if isinstance(item, dict) and item_type in _TOOL_ITEM_TYPES:
            await self._emit_completed_tool(frame, item, params)
        native_id = (
            item.get("id") if isinstance(item, dict) else None
        ) or params.get("itemId")
        # An id-less completion closes whichever synthetic block is open.
        block_id = str(native_id or self._fallback_block_id or "result")
        if block_id in self._open_output_blocks:
            self._open_output_blocks.discard(block_id)
            if block_id == self._fallback_block_id:
                self._fallback_block_id = None
            await self._emit(
                HarnessEventType.ASSISTANT_COMPLETED,
                turn_ref=self._active,
                data={"block_id": block_id},
                native_payload=frame,
            )

    async def _emit_completed_tool(
        self, frame: dict[str, Any], item: dict[str, Any], params: dict[str, Any]
    ) -> None:
        item_id, item_type = (
            str(item.get("id") or params.get("itemId") or ""),
            str(item.get("type") or ""),
        )
        name = _tool_label(item)
        arguments = _tool_arguments(item.get("arguments") or item.get("input") or {})
        result = item.get("result", item.get("output", item.get("contentItems")))
        error = (
            bool(item.get("error"))
            or str(item.get("status") or "").lower() == "failed"
            or (item_type == "dynamicToolCall" and item.get("success") is False)
        )
        await self._emit(
            HarnessEventType.TOOL_COMPLETED,
            turn_ref=self._active,
            data={
                "tool_call_ref": item_id,
                "label": name or "Tool",
                "outcome": "failed" if error else "succeeded",
            },
            native_payload={
                "_puffo_internal": "tool_result",
                "tool_call_id": item_id,
                "tool_name": name,
                "arguments": arguments,
                "result": result,
                "result_omitted": item_type == "dynamicToolCall" and result is None,
                "is_error": error,
            },
        )

    async def _complete_turn(
        self, frame: dict[str, Any], params: dict[str, Any]
    ) -> None:
        status = str(
            (params.get("turn") or {}).get("status")
            if isinstance(params.get("turn"), dict)
            else params.get("status") or "completed"
        )
        for block_id in tuple(sorted(self._open_output_blocks)):
            await self._emit(
                HarnessEventType.ASSISTANT_COMPLETED,
                turn_ref=self._active,
                data={"block_id": block_id},
                native_payload=frame,
            )
        self._open_output_blocks.clear()
        self._fallback_block_id = None
        outcome = (
            "cancelled"
            if "interrupt" in status
            else "failed"
            if "fail" in status
            else "succeeded"
        )
        data: dict[str, Any] = {"outcome": outcome}
        if self._usage_latest:
            data.update(self._usage_latest)
        await self._emit(
            HarnessEventType.TURN_COMPLETED,
            turn_ref=self._active,
            data=data,
            native_payload=frame,
        )
        # Permission requests are scoped to the sole active turn. Once that
        # turn terminates, the manager deliberately rejects their references;
        # retain no unreachable request payloads in the long-lived Driver.
        self._permission_requests.clear()
        self._active, self._active_native_turn_id = TurnRef(""), ""
        self._reset_usage()

    def _update_usage(self, usage: dict[str, Any]) -> None:
        """Restore the historical per-turn ``last``/``total`` token semantics.

        ``last`` is the latest single request; ``total`` is the whole thread.
        The turn's input/output are non-negative deltas from the first observed
        thread totals, with re-sent cached input excluded, and context comes
        from ``last.totalTokens``. The final values stand at turn/completed.
        """
        context_tokens = _context_tokens(usage)
        if (
            isinstance(context_tokens, int)
            and not isinstance(context_tokens, bool)
            and context_tokens > 0
        ):
            self._usage_latest["context_tokens"] = context_tokens
        last = usage.get("last")
        total = usage.get("total")
        if not isinstance(last, dict) or not isinstance(total, dict):
            return
        try:
            cum_out = int(total.get("outputTokens") or 0)
            cum_in = max(
                0,
                int(total.get("inputTokens") or 0)
                - int(total.get("cachedInputTokens") or 0),
            )
            if self._usage_baseline is None:
                last_in = max(
                    0,
                    int(last.get("inputTokens") or 0)
                    - int(last.get("cachedInputTokens") or 0),
                )
                self._usage_baseline = (
                    cum_out - int(last.get("outputTokens") or 0),
                    cum_in - last_in,
                )
            out_base, in_base = self._usage_baseline
            self._usage_latest["output_tokens"] = max(0, cum_out - out_base)
            self._usage_latest["input_tokens"] = max(0, cum_in - in_base)
        except (TypeError, ValueError):
            return

    def _reset_usage(self) -> None:
        self._usage_baseline = None
        self._usage_latest = {}

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
                driver="codex",
                session_ref=self._session_ref,
                turn_ref=(turn_ref if turn_ref and turn_ref.value else None),
                native_session_id=self._native_session_id,
                native_turn_id=self._active_native_turn_id,
                data=data or {},
                native_payload=native_payload,
            )
        )

    def _require_active(self, turn: TurnRef) -> None:
        if not self._active_native_turn_id or turn != self._active:
            raise RuntimeError("stale or foreign active turn")


def _is_request_id(value: Any) -> bool:
    """JSON-RPC ids are integers or strings; booleans are not routable ids."""
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _integer(value: Any) -> int | None:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _context_tokens(usage: dict[str, Any]) -> int | None:
    """Read the current request footprint from the Codex token snapshot.

    Current app-server schemas nest it under ``last.totalTokens``. Keep the
    historical flattened fallback for older compatible app-server builds.
    """
    last = usage.get("last")
    if isinstance(last, dict):
        value = _integer(last.get("totalTokens"))
        if value is not None:
            return value
    return _integer(usage.get("totalTokens"))


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return value if isinstance(value, dict) else {}


CodexDriver = CodexAppServerDriver
