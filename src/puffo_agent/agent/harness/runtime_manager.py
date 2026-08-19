"""Runtime Manager: sole Driver-event consumer and logical ID owner."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..adapters.base import Adapter, TurnContext, TurnResult
from ..context_controller import (
    CompactionResult,
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
    ToolResultAdmission,
    normalize_context_snapshot,
)
from ..errors import AgentAPIError
from .driver import (
    CompactRequest,
    Driver,
    DriverCapabilities,
    HarnessEvent,
    HarnessEventType,
    PermissionDecision,
    PermissionReceipt,
    PermissionRef,
    RuntimeOpened,
    RuntimeSpec,
    SessionRef,
    TurnInput,
    TurnRef,
    UnsupportedCapability,
)

logger = logging.getLogger(__name__)

# Compaction is a provider-side summarization pass over the whole thread, so
# the per-request timeout is far too tight to bound its completion.
COMPACTION_WAIT_SECONDS = 120.0


class RuntimeStateError(RuntimeError):
    """Internal state-machine contract violation, never retried automatically."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def _native_resume_is_unavailable(exc: Exception) -> bool:
    """Whether the provider explicitly rejected the saved native session."""
    text = str(exc).lower()
    identifies_session = any(
        marker in text
        for marker in (
            "session",
            "thread",
            "conversation",
            "rollout",
            "transcript",
        )
    )
    unavailable = any(
        marker in text
        for marker in (
            "not found",
            "does not exist",
            "failed to find",
            "is gone",
            "no rollout",
            "cannot resume",
            "unable to resume",
            "incompatible",
            "invalid session",
            "invalid thread",
        )
    )
    return identifies_session and unavailable


class _EventStream(AsyncIterator[HarnessEvent]):
    """Subscriber handle that releases its queue whether or not it is iterated.

    An async generator only runs its ``finally`` once it has been started, so a
    subscriber whose turn failed before the first ``__anext__`` would leak its
    queue forever.  Closing this handle always discards the queue.
    """

    def __init__(
        self,
        subscribers: set[asyncio.Queue[HarnessEvent | None]],
        queue: asyncio.Queue[HarnessEvent | None],
    ) -> None:
        self._subscribers = subscribers
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> AsyncIterator[HarnessEvent]:
        return self

    async def __anext__(self) -> HarnessEvent:
        if self._closed:
            raise StopAsyncIteration
        event = await self._queue.get()
        if event is None:
            await self.aclose()
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        self._closed = True
        self._subscribers.discard(self._queue)


class RuntimeManager:
    def __init__(
        self, driver: Driver, spec: RuntimeSpec, *,
        agent_id: str = "", session_ref: SessionRef | None = None,
        native_session_id: str = "",
        driver_name: str = "",
        event_sink: Callable[[HarnessEvent], Awaitable[None]] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
    ):
        self.driver = driver
        self.spec = spec
        self.agent_id = agent_id
        self.event_sink = event_sink
        self.before_start = before_start
        self.session_ref = session_ref or SessionRef(
            f"session_{uuid.uuid4().hex}"
        )
        self.native_session_id = native_session_id
        self.driver_name = driver_name or type(driver).__name__
        self.opened: RuntimeOpened | None = None
        self.active_turn_ref: TurnRef | None = None
        self.native_turn_id = ""
        self._active_driver_turn_ref: TurnRef | None = None
        self._turn_refs: dict[TurnRef, TurnRef] = {}
        self._reader: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[HarnessEvent | None]] = set()
        self._terminal: dict[TurnRef, asyncio.Future[HarnessEvent]] = {}
        self._closed = False
        self._command_lock = asyncio.Lock()
        self._permission_refs: set[PermissionRef] = set()
        self._continuation_admissions: list[ToolResultAdmission] = []
        self._compaction: asyncio.Future[None] | None = None
        # True from a resumed open until its first turn succeeds; an
        # invalid --resume target isn't always a synchronous open failure.
        # See _consume_event_locked.
        self._resume_unconfirmed = False
        self._confirmed_native_session_id = ""

    async def open(self, *, resume: bool = True) -> RuntimeOpened:
        async with self._command_lock:
            return await self._open_locked(resume=resume)

    async def _open_locked(self, *, resume: bool = True) -> RuntimeOpened:
        if self._closed:
            raise RuntimeStateError("runtime is closed")
        if self.opened is not None:
            return self.opened
        native_resume = (
            SessionRef(self.native_session_id)
            if resume and self.native_session_id
            else None
        )
        try:
            opened = await self.driver.open(self.spec, native_resume)
        except Exception as exc:
            try:
                await self.driver.close()
            except Exception:
                logger.exception("failed to close runtime after open failure")
            # Network, auth, rate-limit, and process failures retry the same
            # native session. Only explicit absence/incompatibility permits a
            # transparent fresh native session.
            if native_resume is None or not _native_resume_is_unavailable(exc):
                raise
            logger.warning(
                "%s could not resume native session; starting a fresh session",
                self.driver_name,
                exc_info=True,
            )
            self._clear_native_session()
            try:
                opened = await self.driver.open(self.spec, None)
            except Exception:
                try:
                    await self.driver.close()
                except Exception:
                    logger.exception(
                        "failed to close runtime after fresh open failure"
                    )
                raise
        self.native_session_id = opened.native_session_id
        # Preserve the durable Puffo logical reference independently of the
        # native provider session ID.
        self.opened = replace(opened, session_ref=self.session_ref)
        self._resume_unconfirmed = (
            opened.resumed
            and opened.native_session_id != self._confirmed_native_session_id
        )
        self._reader = asyncio.create_task(self._consume_events())
        if self.agent_id:
            register_runtime_manager(self.agent_id, self)
        return self.opened

    async def start_turn(self, input: TurnInput) -> Any:
        async with self._command_lock:
            if self._closed:
                raise RuntimeStateError("runtime is closed")
            if self.opened is None:
                await self._open_locked()
            if self.active_turn_ref is not None:
                raise RuntimeStateError("one provider turn is already active")
            if self.before_start is not None:
                admitted = self.before_start()
                if inspect.isawaitable(admitted):
                    await admitted
            logical = TurnRef(f"turn_{uuid.uuid4().hex}")
            self.active_turn_ref = logical
            # Production consumes terminals from events(), so a settled future
            # is only released here; retention stays bounded at one completed
            # turn instead of growing for the process lifetime.
            for settled, future in tuple(self._terminal.items()):
                if future.done():
                    self._terminal.pop(settled, None)
            loop = asyncio.get_running_loop()
            self._terminal[logical] = loop.create_future()
            try:
                receipt = await self.driver.start_turn(input)
            except BaseException:
                await self._discard_failed_start_locked(logical, retire=True)
                raise
            if isinstance(receipt, UnsupportedCapability):
                await self._discard_failed_start_locked(logical, retire=False)
                return receipt
            if not receipt.accepted:
                # A negative start receipt can mean the input crossed the
                # process boundary but its acknowledgement did not. Retire the
                # native session so an untracked old turn can never overlap the
                # next logical turn.
                await self._discard_failed_start_locked(logical, retire=True)
                return receipt
            self._active_driver_turn_ref = receipt.turn_ref
            self._turn_refs[receipt.turn_ref] = logical
            self.native_turn_id = receipt.native_turn_id
            return replace(receipt, turn_ref=logical)

    async def _discard_failed_start_locked(
        self, logical: TurnRef, *, retire: bool
    ) -> None:
        self.active_turn_ref = None
        self._active_driver_turn_ref = None
        self.native_turn_id = ""
        self._terminal.pop(logical, None)
        self._permission_refs.clear()
        self._continuation_admissions.clear()
        if not retire:
            return
        try:
            await self._retire_runtime_locked(preserve_session=False)
        except Exception:
            # Preserve the start failure as the caller-visible error while
            # still making the next command open a fresh native session.
            logger.exception("failed to retire runtime after turn start failure")
            self.opened = None
            self._clear_native_session()
            self.session_ref = SessionRef(f"session_{uuid.uuid4().hex}")

    async def steer_turn(self, turn: TurnRef, input: TurnInput) -> Any:
        async with self._command_lock:
            self._validate_active(turn)
            assert self._active_driver_turn_ref is not None
            receipt = await self.driver.steer_turn(
                self._active_driver_turn_ref, input
            )
            if isinstance(receipt, UnsupportedCapability):
                return receipt
            return replace(receipt, turn_ref=turn)

    async def cancel_turn(self, turn: TurnRef) -> Any:
        async with self._command_lock:
            self._validate_active(turn)
            assert self._active_driver_turn_ref is not None
            receipt = await self.driver.cancel_turn(
                self._active_driver_turn_ref
            )
            if isinstance(receipt, UnsupportedCapability):
                return receipt
            return replace(receipt, turn_ref=turn)

    async def resolve_permission(
        self, turn: TurnRef, permission: PermissionRef,
        decision: PermissionDecision,
    ) -> PermissionReceipt | UnsupportedCapability:
        async with self._command_lock:
            self._validate_active(turn)
            if permission not in self._permission_refs:
                raise RuntimeStateError("unknown or stale permission reference")
            receipt = await self.driver.resolve_permission(permission, decision)
            if not isinstance(receipt, UnsupportedCapability) and receipt.accepted:
                self._permission_refs.discard(permission)
            return receipt

    async def context_status(self) -> Any:
        async with self._command_lock:
            if self._closed:
                raise RuntimeStateError("runtime is closed")
            return await self.driver.context_status()

    async def begin_compaction(
        self, request: CompactRequest | None = None
    ) -> asyncio.Future[None] | UnsupportedCapability:
        """Start at most one provider compaction and hand back its completion.

        A caller arriving while one is still outstanding is coalesced onto the
        same future rather than issuing a second provider request: one context
        decision must never run two concurrent summarization passes.
        """
        async with self._command_lock:
            if self._closed:
                raise RuntimeStateError("runtime is closed")
            if self._compaction is not None and not self._compaction.done():
                return self._compaction
            receipt = await self.driver.compact(request or CompactRequest())
            if isinstance(receipt, UnsupportedCapability):
                return receipt
            self._compaction = asyncio.get_running_loop().create_future()
            return self._compaction

    def _resolve_compaction_locked(self) -> None:
        future, self._compaction = self._compaction, None
        if future is not None and not future.done():
            future.set_result(None)

    def _fail_compaction_locked(self, reason: str) -> None:
        future, self._compaction = self._compaction, None
        if future is not None and not future.done():
            future.set_exception(RuntimeStateError(reason))
            # Mark retrieved: a shielded waiter still receives the error, but
            # an unwatched future must not log "exception was never retrieved".
            future.exception()

    async def wait_terminal(self, turn: TurnRef) -> HarnessEvent:
        future = self._terminal.get(turn)
        if future is None:
            raise RuntimeStateError("unknown turn reference")
        try:
            return await asyncio.shield(future)
        finally:
            if future.done():
                self._terminal.pop(turn, None)

    async def reload_resources(
        self,
        *,
        preserve_session: bool,
        spec: RuntimeSpec | None = None,
    ) -> None:
        async with self._command_lock:
            if self.active_turn_ref is not None:
                raise RuntimeStateError("cannot reload while a turn is active")
            if spec is not None:
                self.spec = spec
            await self._retire_runtime_locked(
                preserve_session=preserve_session
            )
            await self._open_locked(resume=preserve_session)

    async def abandon_turn(
        self,
        turn: TurnRef,
        *,
        reason: str,
        retryable: bool = True,
    ) -> HarnessEvent:
        """Publish one canonical terminal and release the active Turn."""
        async with self._command_lock:
            self._validate_active(turn)
            event = HarnessEvent(
                type=HarnessEventType.TURN_ABANDONED,
                driver=self.driver_name,
                session_ref=self.session_ref,
                turn_ref=turn,
                native_session_id=self.native_session_id,
                native_turn_id=self.native_turn_id,
                data={
                    "outcome": "abandoned",
                    "error_code": reason,
                    "retryable": retryable,
                },
            )
            await self._publish_terminal_locked(event, turn)
            return event

    async def timeout_turn(
        self,
        turn: TurnRef,
        *,
        cancel_timeout: float = 1.0,
    ) -> HarnessEvent | None:
        """Atomically abandon a timed-out Turn and retire its runtime."""
        async with self._command_lock:
            if self.active_turn_ref != turn:
                return None
            assert self._active_driver_turn_ref is not None
            try:
                await asyncio.wait_for(
                    self.driver.cancel_turn(self._active_driver_turn_ref),
                    timeout=cancel_timeout,
                )
            except Exception:
                logger.info(
                    "provider turn %s did not acknowledge timeout interrupt",
                    turn,
                    exc_info=True,
                )
            event = HarnessEvent(
                type=HarnessEventType.TURN_ABANDONED,
                driver=self.driver_name,
                session_ref=self.session_ref,
                turn_ref=turn,
                native_session_id=self.native_session_id,
                native_turn_id=self.native_turn_id,
                data={
                    "outcome": "abandoned",
                    "error_code": "turn_timeout",
                    "retryable": True,
                },
            )
            try:
                await self._publish_terminal_locked(event, turn)
            finally:
                await self._retire_runtime_locked(preserve_session=False)
            return event

    async def retire_runtime(self, *, preserve_session: bool) -> None:
        """Stop a provider runtime without eagerly opening its replacement."""
        async with self._command_lock:
            if self.active_turn_ref is not None:
                raise RuntimeStateError("cannot retire while a turn is active")
            await self._retire_runtime_locked(
                preserve_session=preserve_session
            )

    async def _retire_runtime_locked(self, *, preserve_session: bool) -> None:
        if self.active_turn_ref is not None:
            raise RuntimeStateError("cannot retire while a turn is active")
        self._fail_compaction_locked("runtime was retired")
        await self._stop_reader()
        await self.driver.close()
        self.opened = None
        if not preserve_session:
            self.session_ref = SessionRef(f"session_{uuid.uuid4().hex}")
            self._clear_native_session()

    def events(self) -> AsyncIterator[HarnessEvent]:
        queue: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self._subscribers.add(queue)
        return _EventStream(self._subscribers, queue)

    async def close(self) -> None:
        async with self._command_lock:
            if self._closed:
                return
            self._closed = True
            if self.agent_id:
                unregister_runtime_manager(self.agent_id, self)
            self._fail_compaction_locked("runtime is closed")
            await self._stop_reader()
            await self.driver.close()
            self.opened = None
            for future in tuple(self._terminal.values()):
                if not future.done():
                    future.set_exception(RuntimeStateError("runtime is closed"))
                    # Mark retrieved: a shielded wait_terminal waiter still
                    # receives the error, but an unwatched future must not log
                    # "exception was never retrieved" at collection.
                    future.exception()
            self._terminal.clear()
            for queue in tuple(self._subscribers):
                queue.put_nowait(None)

    async def _stop_reader(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None

    async def _consume_events(self) -> None:
        try:
            async for native in self.driver.events():
                event_type = getattr(native.type, "value", native.type)
                if event_type in {
                    "runtime.ready",
                    "runtime.warning",
                    "session.opened",
                    "session.resumed",
                }:
                    self.driver_name = native.driver or self.driver_name
                    await self._publish_event(replace(
                        native,
                        session_ref=self.session_ref,
                        turn_ref=None,
                    ))
                    continue
                async with self._command_lock:
                    should_stop = await self._consume_event_locked(native)
                if should_stop:
                    return
            if not self._closed:
                async with self._command_lock:
                    await self._fail_runtime_locked(
                        "runtime_event_stream_ended"
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("provider runtime event processing failed")
            async with self._command_lock:
                await self._fail_runtime_locked(
                    "runtime_event_processing_failed"
                )
        finally:
            if self._reader is asyncio.current_task():
                self._reader = None

    async def _consume_event_locked(self, native: HarnessEvent) -> bool:
        self.driver_name = native.driver or self.driver_name
        logical_turn = (
            self._turn_refs.get(native.turn_ref)
            if native.turn_ref is not None
            else None
        )
        event = replace(
            native, session_ref=self.session_ref, turn_ref=logical_turn
        )
        await self._admit_matching_tool_result(native)
        if event.type in {
            HarnessEventType.PERMISSION_REQUESTED,
            "turn.permission_requested",
        }:
            value = event.data.get("permission_ref")
            if value:
                self._permission_refs.add(PermissionRef(str(value)))
        if event.type in {
            HarnessEventType.COMPACTION_COMPLETED,
            "compaction.completed",
        }:
            # The single consumption point for provider completion; the event
            # still publishes so subscribers and persistence see it.
            self._resolve_compaction_locked()
        if event.type in {
            HarnessEventType.RUNTIME_EXITED,
            "runtime.exited",
        }:
            self._fail_compaction_locked("runtime exited")
            await self._publish_event(event)
            active = self.active_turn_ref
            # A crash before this resumed session's first success is
            # treated the same as the turn.completed case below: discard
            # the session id so the retry gets a fresh session.
            unconfirmed = self._resume_unconfirmed
            self._resume_unconfirmed = False
            failed_native_session_id = self.native_session_id
            if unconfirmed:
                # The event retains the failed native id for diagnostics, but
                # durable manager state must already point at a fresh session.
                self._clear_native_session()
            try:
                if active is not None:
                    abandoned = HarnessEvent(
                        type=HarnessEventType.TURN_ABANDONED,
                        driver=self.driver_name,
                        session_ref=self.session_ref,
                        turn_ref=active,
                        native_session_id=failed_native_session_id,
                        native_turn_id=self.native_turn_id,
                        occurred_at=event.occurred_at,
                        data={
                            "outcome": "abandoned",
                            "error_code": (
                                "resume_unconfirmed" if unconfirmed
                                else "runtime_exited"
                            ),
                            "retryable": True,
                        },
                    )
                    await self._publish_terminal_locked(abandoned, active)
            finally:
                try:
                    await self.driver.close()
                finally:
                    self.opened = None
            return True
        if event.type in {
            HarnessEventType.TURN_COMPLETED, "turn.completed",
        } and logical_turn is not None:
            outcome = str(event.data.get("outcome") or "succeeded")
            if outcome == "succeeded":
                self._resume_unconfirmed = False
                self._confirmed_native_session_id = self.native_session_id
            elif event.data.get("error_code") == "invalid_resume":
                await self._retire_invalid_resume_locked(event, logical_turn)
                return True
        if event.type in {
            HarnessEventType.TURN_COMPLETED,
            HarnessEventType.TURN_ABANDONED,
            "turn.completed",
            "turn.abandoned",
        } and logical_turn is not None:
            await self._publish_terminal_locked(event, logical_turn)
        else:
            await self._publish_event(event)
        return False

    async def _retire_invalid_resume_locked(
        self, event: HarnessEvent, logical_turn: TurnRef
    ) -> None:
        # Claude can report an unusable --resume target only after a user
        # frame. The explicit error remains authoritative even when this
        # session succeeded before a process restart.
        self._resume_unconfirmed = False
        self._fail_compaction_locked("resume unconfirmed")
        terminal = replace(event, data={**event.data, "retryable": True})
        self._clear_native_session()
        try:
            await self._publish_terminal_locked(terminal, logical_turn)
        finally:
            try:
                await self.driver.close()
            finally:
                self.opened = None

    async def _publish_event(self, event: HarnessEvent) -> None:
        if self.event_sink is not None:
            await self.event_sink(event)
        self._fanout_event(event)

    async def _publish_terminal_locked(
        self, event: HarnessEvent, turn: TurnRef
    ) -> None:
        delivered = event
        persistence_error: Exception | None = None
        try:
            if self.event_sink is not None:
                await self.event_sink(event)
        except Exception as exc:
            persistence_error = exc
            delivered = HarnessEvent(
                type=HarnessEventType.TURN_ABANDONED,
                driver=event.driver,
                session_ref=event.session_ref,
                turn_ref=turn,
                native_session_id=event.native_session_id,
                native_turn_id=event.native_turn_id,
                occurred_at=event.occurred_at,
                data={
                    "outcome": "abandoned",
                    "error_code": "event_persistence_failed",
                    "retryable": True,
                },
            )
        finally:
            self._fanout_event(delivered)
            self._complete_turn(delivered, turn)
        if persistence_error is not None:
            raise persistence_error

    def _fanout_event(self, event: HarnessEvent) -> None:
        for queue in tuple(self._subscribers):
            queue.put_nowait(event)

    async def _fail_runtime_locked(self, reason: str) -> None:
        active = self.active_turn_ref
        self._fail_compaction_locked(reason)
        try:
            if active is not None:
                abandoned = HarnessEvent(
                    type=HarnessEventType.TURN_ABANDONED,
                    driver=self.driver_name,
                    session_ref=self.session_ref,
                    turn_ref=active,
                    native_session_id=self.native_session_id,
                    native_turn_id=self.native_turn_id,
                    data={
                        "outcome": "abandoned",
                        "error_code": reason,
                        "retryable": True,
                    },
                )
                try:
                    await self._publish_terminal_locked(abandoned, active)
                except Exception:
                    logger.exception(
                        "failed to persist terminal runtime event after %s",
                        reason,
                    )
            await self.driver.close()
        except Exception:
            logger.exception("failed to close provider runtime after %s", reason)
        finally:
            self.opened = None

    def _complete_turn(self, event: HarnessEvent, turn: TurnRef) -> None:
        future = self._terminal.get(turn)
        if future is not None and not future.done():
            future.set_result(event)
        if self._active_driver_turn_ref is not None:
            self._turn_refs.pop(self._active_driver_turn_ref, None)
        self.active_turn_ref = None
        self._active_driver_turn_ref = None
        self.native_turn_id = ""
        self._permission_refs.clear()
        self._continuation_admissions.clear()

    def register_continuation(
        self,
        callback,
        planning_cycle_key: str,
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: dict[str, object] | None = None,
        correlation_receipt: str = "",
    ) -> None:
        if self.active_turn_ref is None or not self.native_turn_id:
            raise RuntimeStateError(
                "no active provider turn for tool-result admission"
            )
        self._continuation_admissions.append(ToolResultAdmission.build(
            callback,
            planning_cycle_key,
            self.native_turn_id,
            channel_id=channel_id,
            tool_names=tool_names,
            tool_arguments=tool_arguments,
            correlation_receipt=correlation_receipt,
        ))

    async def _admit_matching_tool_result(self, event: HarnessEvent) -> None:
        fact = event.native_diagnostic
        if (
            event.type not in {
                HarnessEventType.TOOL_COMPLETED,
                "turn.tool_completed",
            }
            or event.native_session_id != self.native_session_id
            or event.native_turn_id != self.native_turn_id
            or not isinstance(fact, dict)
            or fact.get("_puffo_internal") != "tool_result"
            or fact.get("is_error") is True
        ):
            return
        tool_name = str(fact.get("tool_name") or "")
        arguments = fact.get("arguments")
        if not isinstance(arguments, dict):
            return
        matching_candidates = [
            (index, admission)
            for index, admission in enumerate(self._continuation_admissions)
            if admission.provider_turn_id == event.native_turn_id
            and admission.matches(tool_name, arguments)
            and admission.receipt_marker
        ]
        exact_candidates = [
            (index, admission)
            for index, admission in matching_candidates
            if admission.receipt_marker in repr(fact.get("result"))
        ]
        candidates = exact_candidates
        # Current Codex app-server dynamicToolCall completion events can omit
        # contentItems even after the model has received the tool result.  In
        # that provider-specific shape, the completion event itself is still
        # valid admission evidence when the active turn has exactly one
        # semantic match.  Never guess if the provider returned a conflicting
        # result or if multiple registrations match the same call.
        if not candidates and fact.get("result_omitted") is True:
            if len(matching_candidates) == 1:
                candidates = matching_candidates
        if not candidates and self._continuation_admissions:
            logger.warning(
                "provider tool result did not match an admission "
                "native_turn=%s active_native_turn=%s tool=%s "
                "argument_keys=%s candidate_count=%d semantic_matches=%d",
                event.native_turn_id,
                self.native_turn_id,
                tool_name,
                sorted(str(key) for key in arguments),
                len(self._continuation_admissions),
                len(matching_candidates),
            )
        if not candidates:
            return
        index, admission = max(candidates, key=lambda value: value[1].match_specificity)
        self._continuation_admissions.pop(index)
        await admission.callback(ProviderAdmissionEvent(
            planning_cycle_key=admission.planning_cycle_key,
            provider_session_id=self.native_session_id,
            provider_turn_id=event.native_turn_id,
            tool_call_id=str(fact.get("tool_call_id") or ""),
            admitted_at=datetime.now(timezone.utc),
        ))

    def _validate_active(self, turn: TurnRef) -> None:
        if not isinstance(turn, TurnRef) or self.active_turn_ref != turn:
            raise RuntimeStateError("stale or foreign turn reference")

    def _clear_native_session(self) -> None:
        cleared = self.native_session_id
        self.native_session_id = ""
        if self._confirmed_native_session_id == cleared:
            self._confirmed_native_session_id = ""

    def current_capabilities(self) -> DriverCapabilities | None:
        dynamic = self.driver.current_capabilities()
        if dynamic is not None:
            return dynamic
        return self.opened.capabilities if self.opened is not None else None


class RuntimeManagerAdapter(Adapter):
    """Blocking compatibility facade over the event-driven Runtime Manager."""

    def __init__(
        self,
        manager: RuntimeManager,
        *,
        spec_reloader: Callable[[str], Awaitable[RuntimeSpec]] | None = None,
        compaction_wait_seconds: float = COMPACTION_WAIT_SECONDS,
        post_close: Callable[[], Awaitable[None]] | None = None,
    ):
        self.manager = manager
        self.spec_reloader = spec_reloader
        self.compaction_wait_seconds = compaction_wait_seconds
        # Optional bounded lifecycle owner hook awaited after the manager
        # (and its Driver) close. Used by the Docker Codex runtime to stop
        # the per-agent container once the exec transport has terminated.
        self.post_close = post_close
        self.assistant_text_parts: list[str] = []
        self._latest_context_limits: tuple[int | None, int | None] = (
            None,
            None,
        )

    @staticmethod
    def _current_user_input(ctx: TurnContext) -> str:
        if not ctx.messages:
            raise RuntimeStateError("turn context requires a current user input")
        current = ctx.messages[-1]
        if not isinstance(current, dict) or current.get("role") != "user":
            raise RuntimeStateError(
                "turn context must end with the current user input"
            )
        message = current.get("content")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeStateError("current user input must be non-empty text")
        return message

    async def _announce_admission(self, started) -> None:
        callback = getattr(self, "_context_admission_callback", None)
        if callback is None:
            return
        await self._fire_admission_callback(ProviderAdmissionEvent(
            planning_cycle_key=getattr(
                self, "_context_admission_planning_cycle_key", ""
            ),
            provider_session_id=self.get_provider_session_id(),
            provider_turn_id=started.native_turn_id or None,
            admitted_at=datetime.now(timezone.utc),
        ))

    async def _consume_turn_events(
        self,
        ctx: TurnContext,
        stream,
        turn: TurnRef,
        metadata: dict[str, Any],
    ) -> TurnResult | None:
        """Drain the turn's events; None means the runtime stream ended."""
        async for event in stream:
            if event.turn_ref != turn:
                continue
            event_type = (
                event.type.value
                if isinstance(event.type, HarnessEventType) else event.type
            )
            if event_type == "turn.assistant_delta":
                text = event.data.get("text")
                if isinstance(text, str):
                    self.assistant_text_parts.append(text)
                    if ctx.on_progress is not None:
                        await ctx.on_progress(text)
            if event_type in {"turn.completed", "turn.abandoned"}:
                outcome = str(event.data.get("outcome") or "succeeded")
                if event_type == "turn.abandoned" or outcome != "succeeded":
                    error_code = str(event.data.get("error_code") or outcome)
                    message = (
                        f"provider turn ended with outcome {outcome} "
                        f"(error_code={error_code})"
                    )
                    if event.data.get("retryable"):
                        raise AgentAPIError(message, error_code=error_code)
                    raise RuntimeStateError(message, error_code=error_code)
                metadata.update({
                    key: value for key, value in event.data.items()
                    if key in {
                        "input_tokens", "output_tokens", "tool_calls",
                        "provider_session_id", "send_message_targets",
                        "context_tokens",
                    }
                })
                return TurnResult(
                    reply="".join(self.assistant_text_parts),
                    input_tokens=int(metadata.get("input_tokens", 0)),
                    output_tokens=int(metadata.get("output_tokens", 0)),
                    tool_calls=int(metadata.get("tool_calls", 0)),
                    metadata=metadata,
                )
        return None

    async def _refresh_terminal_context(self, metadata: dict[str, Any]) -> None:
        """Prefer one fresh native snapshot over provider-specific estimates."""
        try:
            status = await self.manager.context_status()
        except Exception:  # noqa: BLE001 - telemetry must not fail the turn
            logger.debug("post-turn context refresh failed", exc_info=True)
            return
        if isinstance(status, UnsupportedCapability) or status.stale:
            return
        used_tokens = status.used_tokens
        if (
            isinstance(used_tokens, int)
            and not isinstance(used_tokens, bool)
            and used_tokens > 0
        ):
            metadata["context_tokens"] = used_tokens
        context_window = status.context_window
        if (
            isinstance(context_window, int)
            and not isinstance(context_window, bool)
            and context_window > 0
        ):
            metadata["context_window"] = context_window
            pct = self.manager.spec.auto_compact_threshold_pct
            threshold = (
                int(context_window * pct / 100)
                if pct is not None
                else (
                    self.manager.spec.auto_compact_threshold_tokens
                    or status.auto_compact_threshold_tokens
                )
            )
            self._latest_context_limits = (context_window, threshold)
        if status.measured_at:
            metadata["context_measured_at"] = status.measured_at

    async def _timed_out_turn_result(
        self,
        turn: TurnRef | None,
        timeout_seconds: float,
        metadata: dict[str, Any],
    ) -> TurnResult:
        logger.warning(
            "provider turn %s exceeded %.3fs timeout",
            turn,
            timeout_seconds,
        )
        if turn is not None:
            # A start_turn that timed out already released its own
            # bookkeeping through the manager's BaseException path.
            await self.manager.timeout_turn(turn)
        label = (
            f"{timeout_seconds / 60:g} minute"
            if timeout_seconds >= 60 and timeout_seconds % 60 == 0
            else f"{timeout_seconds:g} second"
        )
        return TurnResult(
            reply=f"Task exceeded the {label} timeout.",
            metadata={
                **metadata,
                "runtime_turn_timeout": True,
            },
        )

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        message = self._current_user_input(ctx)
        self.assistant_text_parts = []
        metadata: dict[str, Any] = {}
        timeout_seconds = float(
            getattr(
                getattr(self.manager, "spec", None),
                "task_timeout_seconds",
                600.0,
            )
        )
        # Subscribe before the turn starts so no event can be missed, and let
        # the timeout cover start_turn itself: provider silence on turn/start
        # must not leave the Agent turn active forever.
        stream = self.manager.events()
        turn: TurnRef | None = None
        completed: TurnResult | None = None
        timeout = asyncio.timeout(timeout_seconds)
        try:
            async with timeout:
                started = await self.manager.start_turn(TurnInput(message))
                if (
                    isinstance(started, UnsupportedCapability)
                    or not started.accepted
                ):
                    return TurnResult(reply="", metadata={"accepted": False})
                turn = started.turn_ref
                metadata["turn_ref"] = str(turn)
                await self._announce_admission(started)
                completed = await self._consume_turn_events(
                    ctx, stream, turn, metadata
                )
        except TimeoutError:
            if not timeout.expired():
                raise
            return await self._timed_out_turn_result(
                turn, timeout_seconds, metadata
            )
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()
        if completed is not None:
            # The provider Turn is already terminal. Context telemetry has its
            # own bounded timeout and must not retroactively fail that Turn.
            await self._refresh_terminal_context(completed.metadata)
            return completed
        return TurnResult(reply="", metadata={"stream_error": "runtime_exited"})

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        # A preserved native session already contains the original input and
        # only needs a cheap continuation. A retired session needs the exact
        # durable fallback because its replacement has no prior transcript.
        content = (
            kick_text
            if self.manager.native_session_id
            else fallback_user_message
        )
        return await self.run_turn(replace(
            ctx,
            messages=[{"role": "user", "content": content}],
        ))

    def register_continuation_callback(
        self,
        callback,
        planning_cycle_key: str = "",
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: dict[str, object] | None = None,
        correlation_receipt: str = "",
    ) -> None:
        if callback is None:
            self.manager._continuation_admissions.clear()
            return
        self.manager.register_continuation(
            callback,
            planning_cycle_key,
            channel_id=channel_id,
            tool_names=tool_names,
            tool_arguments=tool_arguments,
            correlation_receipt=correlation_receipt,
        )

    async def warm(self, system_prompt: str) -> None:
        await self.manager.open()

    async def reload(
        self, new_system_prompt: str, *, with_session: bool = False
    ) -> None:
        spec = (
            await self.spec_reloader(new_system_prompt)
            if self.spec_reloader is not None else None
        )
        await self.manager.reload_resources(
            preserve_session=not with_session,
            spec=spec,
        )

    async def aclose(self) -> None:
        try:
            await self.manager.close()
        finally:
            if self.post_close is not None:
                await self.post_close()

    def get_provider_session_id(self) -> str | None:
        value = (
            self.manager.opened.native_session_id
            if self.manager.opened is not None
            else self.manager.native_session_id
        )
        return value or None

    def inbox_notice_delivery_capability(self) -> str:
        capabilities = self.manager.current_capabilities()
        steer = (
            capabilities.steer
            if capabilities is not None
            else getattr(self.manager.driver, "static_steer_capability", "none")
        )
        value = getattr(steer, "value", steer)
        if value == "current_turn":
            return "direct"
        if value == "gated":
            return "gated"
        return "next_turn"

    async def offer_inbox_notice(
        self,
        provider_turn_id: str,
        provider_input: str,
    ) -> bool:
        active = self.manager.active_turn_ref
        if active is None or self.manager.native_turn_id != provider_turn_id:
            return False
        receipt = await self.manager.steer_turn(active, TurnInput(provider_input))
        return not isinstance(receipt, UnsupportedCapability) and receipt.accepted

    async def get_context_snapshot(self) -> ContextSnapshot:
        status = await self.manager.context_status()
        if isinstance(status, UnsupportedCapability):
            return await super().get_context_snapshot()
        window = status.context_window
        threshold = (
            self.manager.spec.auto_compact_threshold_tokens
            or status.auto_compact_threshold_tokens
        )
        pct = self.manager.spec.auto_compact_threshold_pct
        if window and pct is not None:
            threshold = int(window * pct / 100)
            if (
                self.manager.active_turn_ref is None
                and self.manager.spec.auto_compact_threshold_tokens != threshold
            ):
                await self.manager.reload_resources(
                    preserve_session=True,
                    spec=replace(
                        self.manager.spec,
                        auto_compact_threshold_tokens=threshold,
                    ),
                )
        self._latest_context_limits = (window, threshold)
        return normalize_context_snapshot(
            used_tokens=status.used_tokens or 0,
            provider_context_window=window,
            measured_at=datetime.now(timezone.utc),
        )

    def context_limits(self) -> tuple[int | None, int | None]:
        return self._latest_context_limits

    def get_context_capabilities(self) -> ContextCapabilities:
        capabilities = self.manager.current_capabilities()
        if capabilities is None:
            return super().get_context_capabilities()
        compact = getattr(capabilities.compact, "value", capabilities.compact)
        context_status = getattr(
            capabilities.context_status, "value", capabilities.context_status
        )
        return ContextCapabilities(
            native_compaction=(
                compact != "none" and self.manager.active_turn_ref is None
            ),
            rollover=False,
            native_measurement=context_status != "none",
            diagnostic="Driver capabilities",
        )

    async def compact_context(self) -> CompactionResult:
        pending = await self.manager.begin_compaction(CompactRequest())
        if isinstance(pending, UnsupportedCapability):
            return CompactionResult(completed=False, diagnostic=pending.diagnostic)
        try:
            # Await outside the command lock: the event consumer needs that
            # lock to resolve the future, so waiting under it would deadlock.
            # Shield so a timed-out waiter never cancels the future a
            # coalesced waiter is still holding.
            await asyncio.wait_for(
                asyncio.shield(pending), self.compaction_wait_seconds
            )
        except asyncio.TimeoutError:
            # Deliberately leave the manager's future pending: it is what
            # keeps a retry from starting a second provider compaction for the
            # same outstanding operation.
            return CompactionResult(
                completed=False,
                provider_session_id=self.get_provider_session_id(),
                diagnostic=(
                    "compaction accepted; no completion event within "
                    f"{self.compaction_wait_seconds:g}s"
                ),
            )
        except RuntimeStateError as exc:
            return CompactionResult(completed=False, diagnostic=str(exc))
        return CompactionResult(
            completed=True,
            provider_session_id=self.get_provider_session_id(),
            diagnostic="compaction completed",
        )


_RUNTIME_MANAGERS: weakref.WeakValueDictionary[str, RuntimeManager] = (
    weakref.WeakValueDictionary()
)


def register_runtime_manager(agent_id: str, manager: RuntimeManager) -> None:
    _RUNTIME_MANAGERS[agent_id] = manager


def unregister_runtime_manager(agent_id: str, manager: RuntimeManager) -> None:
    if _RUNTIME_MANAGERS.get(agent_id) is manager:
        _RUNTIME_MANAGERS.pop(agent_id, None)


def get_runtime_manager(agent_id: str) -> RuntimeManager | None:
    return _RUNTIME_MANAGERS.get(agent_id)
