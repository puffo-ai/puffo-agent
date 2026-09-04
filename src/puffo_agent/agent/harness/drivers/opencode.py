"""OpenCode Driver using one ``run --format json`` child per turn."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ....tasks import spawn
from ...cli_bin import normalize_launch_argv
from ..support.cleanup_errors import (
    CLEANUP_TIMEOUT_SECONDS,
    collect_cleanup_errors,
    raise_collected_errors,
)
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
from .opencode_protocol import (
    build_opencode_run_command,
    opencode_error_detail,
    normalize_opencode_frame,
)
from ..support.redaction import safe_provider_message
from ..support.subprocess_io import (
    drain_subprocess_stream_keeping_tail,
    process_group_spawn_kwargs,
    shutdown_process_tree,
    signal_process_tree,
)


_SERVER_URL_RE = re.compile(rb"listening on (http://[\w.\[\]:-]+)")


def _loopback_server_url(candidate: str) -> str | None:
    """Return a normalized loopback URL or reject untrusted log noise."""
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or host not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


async def _server_url_from_streams(proc: Any) -> str:
    """Return the base URL a fresh ``opencode serve`` announces.

    The announcement line's stream is not contractual, so watch stdout and
    stderr concurrently and take the first match. The caller bounds this
    with a timeout and owns draining the streams afterwards.
    """

    async def scan(stream: Any) -> str:
        while True:
            line = await stream.readline()
            if not line:
                raise RuntimeError("opencode serve exited before announcing its URL")
            match = _SERVER_URL_RE.search(line)
            if match:
                trusted = _loopback_server_url(match.group(1).decode())
                if trusted is not None:
                    return trusted

    tasks = [
        spawn(scan(proc.stdout), name="opencode.serve.stdout_url"),
        spawn(scan(proc.stderr), name="opencode.serve.stderr_url"),
    ]
    try:
        pending = set(tasks)
        failures: list[BaseException] = []
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    return task.result()
                except Exception as exc:
                    failures.append(exc)
        raise failures[0]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _require_summarize_ack(status: int, body: str) -> None:
    """Accept only the documented HTTP 200 + JSON true acknowledgement."""
    if status != 200:
        raise RuntimeError(
            f"summarize returned HTTP {status}: {body[:300]}"
        )
    try:
        acknowledged = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("summarize returned a non-JSON acknowledgement") from exc
    if acknowledged is not True:
        raise RuntimeError(
            f"summarize returned an unexpected acknowledgement: {body[:300]}"
        )


def _window_from_models_output(text: str, model_id: str) -> int | None:
    """Pick ``limit.context`` for ``model_id`` out of ``models --verbose``.

    The output interleaves ``provider/id`` header lines with pretty-printed
    JSON objects; decode each object where it opens and match on its
    ``id`` field rather than trusting line pairing.
    """
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return None
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = start + consumed
        if not isinstance(obj, dict) or obj.get("id") != model_id:
            continue
        limit = obj.get("limit")
        window = limit.get("context") if isinstance(limit, dict) else None
        if isinstance(window, int) and window > 0:
            return window
        return None


OPENCODE_CAPABILITIES = DriverCapabilities(
    session_resume=True,
    inflight_turn_recovery=False,
    steer=SteerCapability.NONE,
    cancel=CancelCapability.TYPED,
    # step_finish frames carry tokens.total — the provider-reported context
    # occupancy of the step's call (verified additive on 1.18.16). The
    # driver absorbs those pushes; nothing here is estimated.
    context_status=ContextStatusCapability.PUSH,
    # Native summarize via a transient `opencode serve` on loopback: the
    # stable POST /session/{id}/summarize surface (the v2 /api/.../compact
    # endpoint is a 503 placeholder on 1.18.16 — do not wire it). The turn
    # path stays PER_TURN_CHILD; the server exists only for the operation.
    compact=CompactCapability.TYPED,
    permission_bridge=False,
    lifecycle=RuntimeLifecycle.PER_TURN_CHILD,
    busy_delivery=BusyDelivery.REJECT,
)

_SHUTDOWN_GRACE_SECONDS = 5.0
_STDERR_TAIL_GRACE_SECONDS = 1.0


class OpenCodeDriver(Driver):
    """Logical session whose only native process exists during a turn.

    This driver is not a trusted ``puffo-v0`` identity-binding entrypoint.
    Profiles requiring that binding must use the ACP driver's validated
    launch seam.
    """

    static_steer_capability = SteerCapability.NONE

    def __init__(
        self,
        process_factory: Callable[..., Any] | None = None,
        *,
        executable_version: str = "",
    ) -> None:
        self.process_factory = process_factory
        self.executable_version = executable_version
        self._spec: RuntimeSpec | None = None
        self._runtime_ref = RuntimeRef(f"runtime_{uuid.uuid4().hex}")
        self._session_ref = SessionRef(f"session_{uuid.uuid4().hex}")
        self._native_session_id = ""
        self._resumed = False
        self._session_announced = False
        self._proc: Any = None
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._turn_generation = 0
        self._last_provider_error = ""
        self._turn_task: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[bytes] | None = None
        self._accepted: asyncio.Future[tuple[str, str]] | None = None
        self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._terminal_reason = ""
        self._provider_failed = False
        self._context = ContextStatus(stale=True)
        self._context_window: int | None = None
        self._compact_task: asyncio.Task[None] | None = None
        self._compact_ref = ""
        self._serve_proc: Any = None
        self._closed = False

    def current_capabilities(self) -> DriverCapabilities:
        return OPENCODE_CAPABILITIES

    async def open(
        self, spec: RuntimeSpec, resume: SessionRef | None = None
    ) -> RuntimeOpened:
        if self._spec is not None:
            raise RuntimeError("driver is already open")
        if self._closed:
            self._closed = False
            self._events = asyncio.Queue()
        self._spec = spec
        self._resumed = resume is not None
        self._native_session_id = str(resume or "")
        self._session_announced = False
        if spec.model:
            # Resolved synchronously ON PURPOSE: any other opencode process
            # on this agent's data dir contends on its sqlite lock, and the
            # loser dies with "database is locked" — measured live, with
            # the victim being the user's first turn when this ran as a
            # background task. At open() no turn child or summarize server
            # exists yet, so this is the one moment the lookup cannot race
            # anything. (A scratch-HOME lookup does not work instead:
            # provider model lists only appear in a configured home.)
            await self._resolve_context_window(spec)
        return RuntimeOpened(
            self._runtime_ref,
            self._session_ref,
            self._native_session_id,
            self._resumed,
            OPENCODE_CAPABILITIES,
            ProtocolDiagnostics(
                executable_version=self.executable_version,
                schema_source="documented-jsonl",
                native_capabilities=("session_resume", "process_cancel"),
            ),
        )

    async def start_turn(self, input: TurnInput):
        if self._spec is None or self._closed:
            raise RuntimeError("driver is not open")
        if self._active.value or self._turn_task is not None:
            raise RuntimeError("one turn is already active")
        if self._compact_task is not None and not self._compact_task.done():
            # The transient summarize server holds opencode's storage; a
            # concurrent `opencode run` would die on its sqlite lock. Turns
            # wait out the compaction instead of losing to it.
            raise RuntimeError("compaction in progress; retry when it completes")
        turn = TurnRef(f"turn_{uuid.uuid4().hex}")
        self._active = turn
        self._active_native_turn_id = ""
        self._provider_failed = False
        self._terminal_reason = ""
        self._last_provider_error = ""
        self._turn_generation += 1
        generation = self._turn_generation
        self._accepted = asyncio.get_running_loop().create_future()
        try:
            self._proc = await self._spawn(self._spec, input.content)
            self._stderr_reader = spawn(
                drain_subprocess_stream_keeping_tail(
                    getattr(self._proc, "stderr", None)
                ),
                name="opencode.stderr",
            )
            self._turn_task = spawn(
                self._drive_turn(self._proc, turn, generation),
                name="opencode.turn",
            )
            native_session_id, native_turn_id = await asyncio.shield(
                self._accepted
            )
        except BaseException as exc:
            errors: list[BaseException] = [exc]
            await collect_cleanup_errors(
                self._abort_failed_start(generation),
                errors,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
            raise_collected_errors("OpenCode turn start cleanup failed", errors)
        return TurnStarted(
            turn,
            native_turn_id=native_turn_id,
            accepted=True,
            delivery="first_json_frame",
        )

    async def _spawn(self, spec: RuntimeSpec, prompt: str) -> Any:
        command = build_opencode_run_command(
            spec,
            prompt=prompt,
            native_session_id=self._native_session_id,
        )
        if self.process_factory is not None:
            # One call with the declared signature. Retrying on TypeError
            # could create a second child when the factory itself failed.
            proc = self.process_factory(command, spec)
            return await proc if asyncio.iscoroutine(proc) else proc
        executable, *arguments = command
        env = dict(spec.environment)
        return await asyncio.create_subprocess_exec(
            *normalize_launch_argv(executable),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.workspace_dir or None,
            env=env,
            limit=16 * 1024 * 1024,
            **process_group_spawn_kwargs(),
        )

    async def steer_turn(self, turn: TurnRef, input: TurnInput):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn: TurnRef):
        self._require_active(turn)
        proc = self._proc
        if proc is None or getattr(proc, "returncode", None) is not None:
            return CancelReceipt(False, turn)
        self._terminal_reason = "cancelled"
        await signal_process_tree(
            proc, force=False, timeout=_SHUTDOWN_GRACE_SECONDS
        )
        return CancelReceipt(True, turn)

    async def context_status(self):
        if self._context.used_tokens is not None and (
            self._context.context_window is None
            and self._context_window is not None
        ):
            # The window resolved after the last push; fold it in.
            self._context = dataclasses.replace(
                self._context, context_window=self._context_window
            )
        return self._context

    async def _resolve_context_window(self, spec: RuntimeSpec) -> None:
        """Resolve ``limit.context`` for spec.model from the model registry.

        ``opencode models <provider> --verbose`` prints, per model, a
        ``provider/id`` line followed by a JSON object carrying
        ``limit.context``. Best-effort: any failure leaves the window None
        — used_tokens still flows, the UI just cannot show a percentage.
        """
        provider, _, model_id = spec.model.partition("/")
        proc = None
        # The registry lookup must NOT share opencode's data storage with the
        # turn children: two opencode processes on one data dir contend on
        # its sqlite lock, and the loser dies with "database is locked" —
        # measured live, with the victim being the user's first turn. The
        # model registry needs no session state, so point data-dir anchors at
        # a throwaway. Preserve HOME, config roots, and the workspace cwd:
        # custom providers/plugins and project opencode.json are part of the
        # real model registry and must remain visible.
        scratch = tempfile.mkdtemp(prefix="oc-models-")
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *normalize_launch_argv(spec.executable),
                    "models",
                    provider,
                    "--verbose",
                    env={
                        **dict(spec.environment),
                        "XDG_DATA_HOME": f"{scratch}/xdg-data",
                        "APPDATA": f"{scratch}/appdata",
                        "LOCALAPPDATA": f"{scratch}/localappdata",
                    },
                    cwd=spec.workspace_dir or None,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=16 * 1024 * 1024,
                    **process_group_spawn_kwargs(),
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except (OSError, asyncio.TimeoutError):
                return
            finally:
                if proc is not None and proc.returncode is None:
                    await shutdown_process_tree(
                        proc,
                        waiter=None,
                        timeout=_SHUTDOWN_GRACE_SECONDS,
                        task_name="opencode.context_window.wait",
                    )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._context_window = _window_from_models_output(
            out.decode("utf-8", "replace"), model_id
        )

    def _absorb_context_update(self, event: HarnessEvent) -> HarnessEvent:
        total = event.data.get("total_tokens")
        if not isinstance(total, int) or total <= 0:
            # A step_finish without a usable total (e.g. an aborted step)
            # must not clobber the last good measurement.
            return event
        self._context = ContextStatus(
            used_tokens=total,
            context_window=self._context_window,
            stale=False,
            measured_at=datetime.now(timezone.utc).isoformat(),
        )
        return dataclasses.replace(
            event,
            data={
                **event.data,
                "used_tokens": total,
                "context_window": self._context_window,
            },
        )

    async def compact(self, request: CompactRequest):
        spec = self._spec
        if spec is None or self._closed:
            raise RuntimeError("driver is not open")
        if self._turn_task is not None and not self._turn_task.done():
            raise RuntimeError("compaction requires an idle session")
        if not self._native_session_id:
            raise RuntimeError("no native session to compact yet")
        if not spec.model:
            raise RuntimeError(
                "OpenCode summarize needs an explicit '<provider>/<model>'"
            )
        if self._compact_task is not None and not self._compact_task.done():
            return CompactReceipt(True, self._compact_ref)
        self._compact_ref = f"compact_{uuid.uuid4().hex}"
        # The manager awaits this method BEFORE it creates the future it
        # resolves on COMPACTION_COMPLETED — completing synchronously here
        # would emit into a void and hang the caller. Hand the work to a
        # task and return; the task yields before it emits anything.
        self._compact_task = spawn(
            self._run_compaction(spec, self._native_session_id, self._compact_ref),
            name="opencode.compact",
        )
        return CompactReceipt(True, self._compact_ref)

    async def _run_compaction(
        self, spec: RuntimeSpec, native_session_id: str, ref: str
    ) -> None:
        await asyncio.sleep(0)
        await self._emit(
            HarnessEventType.COMPACTION_STARTED,
            data={"operation_ref": ref},
        )
        try:
            await self._summarize_via_serve(spec, native_session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                HarnessEventType.COMPACTION_FAILED,
                data={
                    "operation_ref": ref,
                    "diagnostic": safe_provider_message(str(exc)),
                },
            )
            return
        await self._emit(
            HarnessEventType.COMPACTION_COMPLETED,
            data={"operation_ref": ref},
        )

    async def _summarize_via_serve(
        self, spec: RuntimeSpec, native_session_id: str
    ) -> None:
        """Run one summarize against a transient loopback server.

        The server shares the per-turn children's storage (same env, same
        workspace), exists only for this operation, and is spawned as a
        group leader so shutdown takes its whole tree. It is secured with
        a one-shot password even on loopback.
        """
        import aiohttp

        password = uuid.uuid4().hex
        proc = await asyncio.create_subprocess_exec(
            *normalize_launch_argv(spec.executable),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            env={**dict(spec.environment), "OPENCODE_SERVER_PASSWORD": password},
            cwd=spec.workspace_dir or None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **process_group_spawn_kwargs(),
        )
        self._serve_proc = proc
        try:
            base_url = await asyncio.wait_for(
                _server_url_from_streams(proc), timeout=30
            )
            # Keep both pipes drained while the request runs; serve keeps
            # logging and a full pipe would wedge it.
            drains = (
                spawn(
                    drain_subprocess_stream_keeping_tail(proc.stdout),
                    name="opencode.serve.stdout",
                ),
                spawn(
                    drain_subprocess_stream_keeping_tail(proc.stderr),
                    name="opencode.serve.stderr",
                ),
            )
            try:
                provider, _, model_id = spec.model.partition("/")
                timeout = aiohttp.ClientTimeout(total=300)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    response = await http.post(
                        f"{base_url}/session/{native_session_id}/summarize",
                        json={
                            "providerID": provider,
                            "modelID": model_id,
                            "auto": False,
                        },
                        headers={
                            "Authorization": aiohttp.encode_basic_auth(
                                "opencode", password
                            )
                        },
                    )
                    body = await response.text()
                    _require_summarize_ack(response.status, body)
            finally:
                for drain in drains:
                    drain.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        finally:
            try:
                await shutdown_process_tree(
                    proc,
                    waiter=None,
                    timeout=_SHUTDOWN_GRACE_SECONDS,
                    task_name="opencode.serve.wait",
                )
            finally:
                if self._serve_proc is proc:
                    self._serve_proc = None

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
        self._terminal_reason = "runtime_closed"
        proc = self._proc
        errors: list[BaseException] = []
        await collect_cleanup_errors(
            self._settle_turn_task(proc),
            errors,
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
        if self._stderr_reader is not None:
            self._stderr_reader.cancel()
            await collect_cleanup_errors(
                asyncio.gather(self._stderr_reader, return_exceptions=True),
                errors,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
            self._stderr_reader = None
        self._proc = None
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._accepted = None
        self._turn_task = None
        if self._compact_task is not None:
            self._compact_task.cancel()
            await collect_cleanup_errors(
                asyncio.gather(self._compact_task, return_exceptions=True),
                errors,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
            self._compact_task = None
        serve = self._serve_proc
        if serve is not None:
            # Cancellation unwound _summarize_via_serve before its own
            # cleanup ran; take the whole serve tree down here.
            await collect_cleanup_errors(
                shutdown_process_tree(
                    serve,
                    waiter=None,
                    timeout=_SHUTDOWN_GRACE_SECONDS,
                    task_name="opencode.serve.close_wait",
                ),
                errors,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
            self._serve_proc = None
        self._spec = None
        self._context = ContextStatus(stale=True)
        # A reopened driver may carry a different model; the old window
        # must not be paired with the new model's totals while the fresh
        # lookup is still running.
        self._context_window = None
        await collect_cleanup_errors(
            self._events.put(None), errors, timeout=CLEANUP_TIMEOUT_SECONDS
        )
        raise_collected_errors("OpenCode driver close failed", errors)

    async def _drive_turn(
        self, proc: Any, turn: TurnRef, generation: int
    ) -> None:
        read_task = spawn(
            self._read_turn_frames(proc, turn, generation),
            name="opencode.frames",
        )
        wait_task = spawn(proc.wait(), name="opencode.wait")
        try:
            done, _ = await asyncio.wait(
                {read_task, wait_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            if (
                read_task in done
                and not read_task.cancelled()
                and read_task.exception() is not None
                and not wait_task.done()
                and getattr(proc, "returncode", None) is None
            ):
                await signal_process_tree(
                    proc, force=False, timeout=_SHUTDOWN_GRACE_SECONDS
                )
            results = await asyncio.gather(
                read_task, wait_task, return_exceptions=True
            )
            read_result, wait_result = results
            returncode = (
                wait_result
                if isinstance(wait_result, int)
                else getattr(proc, "returncode", None)
            )
            if isinstance(read_result, BaseException):
                self._provider_failed = True
                await self._emit(
                    HarnessEventType.RUNTIME_WARNING,
                    turn=turn,
                    data={"code": "opencode_stream_read"},
                )
            await self._finish_turn(turn, generation, returncode)
        finally:
            for task in (read_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                read_task, wait_task, return_exceptions=True
            )

    async def _read_turn_frames(
        self, proc: Any, turn: TurnRef, generation: int
    ) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._emit(
                    HarnessEventType.RUNTIME_WARNING,
                    turn=turn,
                    data={"code": "protocol_parse"},
                )
                continue
            if not isinstance(frame, dict):
                await self._emit(
                    HarnessEventType.RUNTIME_WARNING,
                    turn=turn,
                    data={"code": "protocol_shape"},
                )
                continue
            if generation != self._turn_generation:
                return
            part = frame.get("part")
            part = part if isinstance(part, dict) else {}
            native_session_id = str(
                frame.get("sessionID") or part.get("sessionID") or ""
            )
            native_turn_id = str(part.get("messageID") or "")
            if not self._accepted or not self._accepted.done():
                if not native_session_id:
                    continue
                if (
                    self._native_session_id
                    and native_session_id != self._native_session_id
                ):
                    raise RuntimeError("OpenCode resumed a different native session")
                was_new = not self._native_session_id
                self._native_session_id = native_session_id
                self._active_native_turn_id = native_turn_id
                if not self._session_announced:
                    await self._emit(
                        (
                            HarnessEventType.SESSION_OPENED
                            if was_new
                            else HarnessEventType.SESSION_RESUMED
                        ),
                        native_payload=frame,
                    )
                    self._session_announced = True
                self._accepted.set_result((native_session_id, native_turn_id))
            if str(frame.get("type") or "") == "error":
                detail = opencode_error_detail(frame)
                if detail:
                    self._last_provider_error = detail
            for event in normalize_opencode_frame(
                frame,
                session_ref=self._session_ref,
                turn_ref=turn,
            ):
                if event.type is HarnessEventType.RUNTIME_FAILED:
                    self._provider_failed = True
                if event.type is HarnessEventType.CONTEXT_UPDATED:
                    event = self._absorb_context_update(event)
                await self._events.put(event)

    async def _finish_turn(
        self, turn: TurnRef, generation: int, returncode: int | None
    ) -> None:
        if generation != self._turn_generation:
            return
        # The direct child has exited (or been killed) by the time we get
        # here, but a descendant can still hold the inherited stderr fd.
        # Collect the tail within a strict budget: it often carries the only
        # human-readable cause for a failed start, but diagnostics must never
        # block turn admission or its terminal boundary indefinitely.
        stderr_tail = b""
        if self._stderr_reader is not None:
            stderr_reader = self._stderr_reader
            self._stderr_reader = None
            try:
                done, _ = await asyncio.wait(
                    {stderr_reader}, timeout=_STDERR_TAIL_GRACE_SECONDS
                )
            except asyncio.CancelledError:
                stderr_reader.cancel()
                raise
            if stderr_reader in done:
                try:
                    result = stderr_reader.result()
                except (asyncio.CancelledError, Exception):
                    pass
                else:
                    if isinstance(result, bytes):
                        stderr_tail = result
            else:
                # Do not await cancellation here: a misbehaving drain must
                # not turn this bounded diagnostic path back into a hang.
                stderr_reader.cancel()
        diagnostic = self._last_provider_error
        if not diagnostic and stderr_tail:
            diagnostic = safe_provider_message(
                stderr_tail.decode("utf-8", errors="replace")
            )
        accepted = self._accepted
        if accepted is not None and not accepted.done():
            cause = f": {diagnostic}" if diagnostic else ""
            accepted.set_exception(
                RuntimeError(
                    "OpenCode exited before accepting the turn "
                    f"(returncode={returncode}){cause}"
                )
            )
        reason = self._terminal_reason
        if reason:
            type_ = HarnessEventType.TURN_ABANDONED
            data = {
                "outcome": "abandoned",
                "error_code": reason,
                "retryable": reason != "cancelled",
            }
        elif returncode not in (None, 0) or self._provider_failed:
            type_ = HarnessEventType.TURN_COMPLETED
            data = {
                "outcome": "failed",
                "error_code": (
                    "opencode_run_error"
                    if self._provider_failed
                    else "opencode_process_exit"
                ),
            }
            if diagnostic:
                data["diagnostic"] = diagnostic
        else:
            type_ = HarnessEventType.TURN_COMPLETED
            data = {"outcome": "succeeded"}
        terminal: HarnessEvent | None = None
        if accepted is not None and accepted.done() and not accepted.cancelled():
            try:
                accepted.result()
            except Exception:
                pass
            else:
                terminal = HarnessEvent.normalized(
                    type=type_,
                    driver="opencode",
                    session_ref=self._session_ref,
                    turn_ref=turn,
                    native_session_id=self._native_session_id,
                    native_turn_id=self._active_native_turn_id,
                    data=data,
                )
        self._proc = None
        self._active = TurnRef("")
        self._active_native_turn_id = ""
        self._accepted = None
        self._turn_task = None
        # Publish only after the Driver is idle.  A fast consumer may start
        # the next turn as soon as it sees this boundary.
        if terminal is not None:
            await self._events.put(terminal)

    async def _abort_failed_start(self, generation: int) -> None:
        proc = self._proc
        task = self._turn_task
        try:
            if task is not None and task is not asyncio.current_task():
                await self._settle_turn_task(proc)
        finally:
            if generation == self._turn_generation:
                self._proc = None
                self._active = TurnRef("")
                self._active_native_turn_id = ""
                self._accepted = None
                self._turn_task = None

    async def _settle_turn_task(self, proc: Any) -> None:
        task = self._turn_task
        if task is asyncio.current_task():
            return
        await shutdown_process_tree(
            proc,
            waiter=task,
            timeout=_SHUTDOWN_GRACE_SECONDS,
            task_name="opencode.shutdown_wait",
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
            HarnessEvent.normalized(
                type=type_,
                driver="opencode",
                session_ref=self._session_ref,
                turn_ref=turn,
                native_session_id=self._native_session_id,
                native_turn_id=self._active_native_turn_id,
                data=data or {},
                native_payload=native_payload,
            )
        )

    def _require_active(self, turn: TurnRef) -> None:
        if turn != self._active or not self._active.value:
            raise RuntimeError("stale or foreign active turn")


OpenCodeCliDriver = OpenCodeDriver
