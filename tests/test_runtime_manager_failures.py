from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.errors import AgentAPIError
from puffo_agent.agent.adapters.base import TurnContext, TurnResult
from puffo_agent.agent.harness.codex_driver import CODEX_CAPABILITIES
from puffo_agent.agent.harness.driver import (
    CancelReceipt,
    CompactReceipt,
    ContextStatus,
    Driver,
    HarnessEvent,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnInput,
    TurnRef,
    TurnStarted,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
    RuntimeStateError,
)
from puffo_agent.agent.runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
)
from puffo_agent.agent.runtime_events import RuntimeEventProjector


class _ControllableDriver(Driver):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        self.started = asyncio.Event()
        self.open_calls = 0
        self.start_calls = 0
        self.cancel_calls = 0
        self.close_calls = 0
        self.turn = TurnRef("")

    async def open(self, spec, resume=None):
        self.open_calls += 1
        self.queue = asyncio.Queue()
        native_session = (
            str(resume) if resume else f"native-session-{self.open_calls}"
        )
        return RuntimeOpened(
            RuntimeRef(f"runtime-{self.open_calls}"),
            SessionRef(native_session),
            native_session,
            bool(resume),
            CODEX_CAPABILITIES,
            SimpleNamespace(),
        )

    async def start_turn(self, input):
        self.start_calls += 1
        self.turn = TurnRef(f"driver-turn-{self.start_calls}")
        self.started.set()
        return TurnStarted(self.turn, f"native-turn-{self.start_calls}")

    async def steer_turn(self, turn, input):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn):
        self.cancel_calls += 1
        return CancelReceipt(True, turn)

    async def context_status(self):
        return UnsupportedCapability("context_status")

    async def compact(self, request):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request, decision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                event = await self.queue.get()
                if event is None:
                    return
                yield event

        return iterate()

    async def close(self):
        self.close_calls += 1
        await self.queue.put(None)


class _CompactingDriver(_ControllableDriver):
    """A driver whose `compact` is acceptance only, as both real ones are."""

    def __init__(self) -> None:
        super().__init__()
        self.compact_calls = 0

    async def compact(self, request):
        self.compact_calls += 1
        return CompactReceipt(True, f"compact-{self.compact_calls}")

    async def context_status(self):
        return ContextStatus(used_tokens=12, context_window=100)


async def _open_compacting_manager(*, wait_seconds=0.05):
    driver = _CompactingDriver()
    manager = RuntimeManager(
        driver, RuntimeSpec("/tmp", task_timeout_seconds=1), driver_name="codex"
    )
    await manager.open()
    adapter = RuntimeManagerAdapter(manager, compaction_wait_seconds=wait_seconds)
    return driver, manager, adapter


async def _feed_compaction_completed(driver, manager):
    await driver.queue.put(HarnessEvent(
        type="compaction.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
    ))


class _PausedStartDriver(_ControllableDriver):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start_turn(self, input):
        self.start_calls += 1
        self.turn = TurnRef(f"driver-turn-{self.start_calls}")
        self.started.set()
        self.start_entered.set()
        await self.release_start.wait()
        return TurnStarted(self.turn, f"native-turn-{self.start_calls}")


class _PausedCancelDriver(_ControllableDriver):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_entered = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def cancel_turn(self, turn):
        self.cancel_calls += 1
        self.cancel_entered.set()
        await self.release_cancel.wait()
        return CancelReceipt(True, turn)


class _FailingStartDriver(_ControllableDriver):
    async def start_turn(self, input):
        self.start_calls += 1
        raise RuntimeError("provider refused the turn")


class _RejectingStartDriver(_ControllableDriver):
    async def start_turn(self, input):
        self.start_calls += 1
        return UnsupportedCapability("start_turn")


class _AmbiguousStartDriver(_ControllableDriver):
    async def start_turn(self, input):
        self.start_calls += 1
        return TurnStarted(
            TurnRef("driver-ambiguous"),
            accepted=False,
            delivery="ambiguous_at_least_once",
        )


class _ResumeBoundaryDriver(_ControllableDriver):
    def __init__(self, resume_error: Exception) -> None:
        super().__init__()
        self.resume_error = resume_error
        self.resume_values: list[SessionRef | None] = []

    async def open(self, spec, resume=None):
        self.resume_values.append(resume)
        if resume is not None:
            raise self.resume_error
        return await super().open(spec, resume)


@pytest.mark.asyncio
async def test_native_resume_falls_back_only_when_session_is_missing():
    driver = _ResumeBoundaryDriver(RuntimeError("Codex thread not found"))
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    opened = await manager.open()

    assert driver.resume_values == [SessionRef("native-old"), None]
    assert opened.session_ref == SessionRef("logical-session")
    assert manager.native_session_id == "native-session-1"
    await manager.close()


@pytest.mark.asyncio
async def test_native_resume_keeps_session_on_transient_error():
    driver = _ResumeBoundaryDriver(
        AgentAPIError("provider temporarily unavailable", is_auth=False)
    )
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    with pytest.raises(AgentAPIError, match="temporarily unavailable"):
        await manager.open()
    assert driver.resume_values == [SessionRef("native-old")]
    assert manager.session_ref == SessionRef("logical-session")
    assert manager.native_session_id == "native-old"


def _context():
    return SimpleNamespace(
        messages=[{"role": "user", "content": "current notice"}],
        on_progress=None,
    )


async def _complete_active_turn(manager, driver):
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))
    for _ in range(500):
        if manager.active_turn_ref is None:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("provider turn never reached its terminal")


@pytest.mark.asyncio
async def test_runtime_exit_abandons_active_turn_and_allows_next_turn():
    driver = _ControllableDriver()
    persisted: list[HarnessEvent] = []

    async def persist(event):
        persisted.append(event)

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
        event_sink=persist,
    )
    adapter = RuntimeManagerAdapter(manager)

    first = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="runtime.exited",
        driver="codex",
        session_ref=SessionRef("native-session-1"),
    ))

    with pytest.raises(AgentAPIError, match="outcome abandoned") as excinfo:
        await asyncio.wait_for(first, timeout=1)
    # The Global Inbox retry path only fires for is_auth=False AgentAPIError.
    assert excinfo.value.is_auth is False
    assert excinfo.value.error_code == "runtime_exited"
    assert manager.active_turn_ref is None
    assert manager.opened is None
    assert [str(event.type) for event in persisted][-1].endswith(
        "TURN_ABANDONED"
    )

    driver.started = asyncio.Event()
    second = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef("native-session-2"),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))
    assert (await asyncio.wait_for(second, timeout=1)).reply == ""
    assert driver.start_calls == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_first_turn_failure_after_resume_forces_a_fresh_session():
    driver = _ControllableDriver()
    durable_session_ids: list[str] = []

    async def persist_terminal(event):
        if getattr(event.type, "value", event.type) == "turn.completed":
            durable_session_ids.append(manager.native_session_id)

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="claude-code",
        native_session_id="stale-session-id",
        event_sink=persist_terminal,
    )
    adapter = RuntimeManagerAdapter(manager)

    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    assert driver.open_calls == 1

    # claude-code doesn't fail driver.open() on an invalid --resume target --
    # it reports "No conversation found" as an ordinary failed turn.completed.
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "failed", "error_code": "invalid_resume"},
    ))

    with pytest.raises(AgentAPIError, match="invalid_resume") as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.is_auth is False
    assert excinfo.value.error_code == "invalid_resume"
    assert durable_session_ids == [""]
    assert manager.native_session_id == ""
    assert manager.opened is None
    assert manager._resume_unconfirmed is False

    # The next open is a fresh one: no resume target survives the failure.
    driver.started = asyncio.Event()
    second = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    assert driver.open_calls == 2
    assert manager.opened.resumed is False
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))
    assert (await asyncio.wait_for(second, timeout=1)).reply == ""
    await adapter.aclose()


@pytest.mark.asyncio
async def test_runtime_crash_on_unconfirmed_resume_also_forces_a_fresh_session():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
        native_session_id="stale-session-id",
    )
    adapter = RuntimeManagerAdapter(manager)

    # Some broken resume targets crash the whole process (RUNTIME_EXITED)
    # instead of returning a clean failed turn.completed like the test above.
    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="runtime.exited",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
    ))

    with pytest.raises(AgentAPIError, match="resume_unconfirmed") as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.is_auth is False
    assert excinfo.value.error_code == "resume_unconfirmed"
    assert manager.native_session_id == ""
    assert manager.opened is None
    assert manager._resume_unconfirmed is False

    driver.started = asyncio.Event()
    second = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    assert driver.open_calls == 2
    assert manager.opened.resumed is False
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))
    assert (await asyncio.wait_for(second, timeout=1)).reply == ""
    await adapter.aclose()


@pytest.mark.asyncio
async def test_confirmed_resumed_session_keeps_later_failures_non_retryable():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
        native_session_id="good-session-id",
    )
    adapter = RuntimeManagerAdapter(manager)

    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))
    assert (await asyncio.wait_for(running, timeout=1)).reply == ""
    assert manager._resume_unconfirmed is False

    # A resource-only reload resumes the same already-confirmed session and
    # must not make its next ordinary provider failure look like a bad resume.
    await manager.reload_resources(preserve_session=True)
    assert manager._resume_unconfirmed is False

    # A confirmed session's later failure must not trigger a fresh-session
    # reset -- it's a genuine rejection, not proof the resume was bad.
    driver.started = asyncio.Event()
    second = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "failed"},
    ))
    with pytest.raises(RuntimeStateError, match="outcome failed"):
        await asyncio.wait_for(second, timeout=1)
    # No additional retire/reopen: unlike an invalid resume, this stays put.
    assert driver.open_calls == 2
    assert manager.opened is not None
    assert manager.native_session_id != ""

    driver.started = asyncio.Event()
    third = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="claude-code",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "failed", "error_code": "invalid_resume"},
    ))
    with pytest.raises(AgentAPIError, match="invalid_resume"):
        await asyncio.wait_for(third, timeout=1)
    assert manager.native_session_id == ""
    await manager.close()


@pytest.mark.asyncio
async def test_provider_reported_failure_without_retryable_flag_stays_terminal():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="claude-code",
        native_session_id="valid-but-unconfirmed-session",
    )
    adapter = RuntimeManagerAdapter(manager)

    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "failed"},
    ))

    # No "retryable" hint: stays a non-retryable RuntimeStateError.
    with pytest.raises(RuntimeStateError, match="outcome failed") as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.error_code == "failed"
    assert manager.active_turn_ref is None
    assert manager.native_session_id == "valid-but-unconfirmed-session"
    assert manager.opened is not None
    await manager.close()


@pytest.mark.asyncio
async def test_retry_payload_depends_on_whether_native_session_survived():
    manager = SimpleNamespace(native_session_id="preserved-session")
    adapter = RuntimeManagerAdapter(manager)
    received: list[str] = []

    async def capture(ctx):
        received.append(ctx.messages[-1]["content"])
        return TurnResult(reply="")

    adapter.run_turn = capture
    context = TurnContext(system_prompt="system", messages=[])

    await adapter.run_retry_turn("continue", "full durable input", context)
    manager.native_session_id = ""
    await adapter.run_retry_turn("continue", "full durable input", context)

    assert received == ["continue", "full durable input"]


@pytest.mark.asyncio
async def test_abandon_turn_marked_non_retryable_stays_a_runtime_state_error():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver, RuntimeSpec("/tmp", task_timeout_seconds=1), driver_name="codex"
    )
    adapter = RuntimeManagerAdapter(manager)

    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    while manager.active_turn_ref is None:
        await asyncio.sleep(0)

    await manager.abandon_turn(
        manager.active_turn_ref, reason="policy_rejected", retryable=False
    )

    with pytest.raises(RuntimeStateError, match="outcome abandoned") as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.error_code == "policy_rejected"
    await manager.close()


@pytest.mark.asyncio
async def test_turn_timeout_interrupts_abandons_and_retires_runtime():
    driver = _ControllableDriver()
    persisted: list[HarnessEvent] = []

    async def persist(event):
        persisted.append(event)

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=0.01),
        driver_name="codex",
        event_sink=persist,
    )
    result = await asyncio.wait_for(
        RuntimeManagerAdapter(manager).run_turn(_context()), timeout=1
    )

    assert result.metadata["runtime_turn_timeout"] is True
    assert result.reply == "Task exceeded the 0.01 second timeout."
    assert driver.cancel_calls == 1
    assert driver.close_calls == 1
    assert manager.active_turn_ref is None
    assert manager.opened is None
    terminal = persisted[-1]
    assert str(terminal.type).endswith("TURN_ABANDONED")
    assert terminal.data["error_code"] == "turn_timeout"


@pytest.mark.asyncio
async def test_runtime_exit_waits_for_start_registration_before_abandoning():
    driver = _PausedStartDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )
    running = asyncio.create_task(
        RuntimeManagerAdapter(manager).run_turn(_context())
    )

    await asyncio.wait_for(driver.start_entered.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="runtime.exited",
        driver="codex",
        session_ref=SessionRef("native-session-1"),
    ))
    await asyncio.sleep(0)
    driver.release_start.set()

    with pytest.raises(AgentAPIError, match="outcome abandoned"):
        await asyncio.wait_for(running, timeout=1)
    assert manager.active_turn_ref is None
    assert manager.opened is None


@pytest.mark.asyncio
async def test_timeout_cleanup_cannot_retire_the_next_turn_runtime():
    driver = _PausedCancelDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )
    first = await manager.start_turn(TurnInput("first"))
    cleanup = asyncio.create_task(manager.timeout_turn(first.turn_ref))
    await asyncio.wait_for(driver.cancel_entered.wait(), timeout=1)

    replacement = asyncio.create_task(manager.start_turn(TurnInput("next")))
    await asyncio.sleep(0)
    assert replacement.done() is False

    driver.release_cancel.set()
    await asyncio.wait_for(cleanup, timeout=1)
    second = await asyncio.wait_for(replacement, timeout=1)

    assert driver.open_calls == 2
    assert manager.opened is not None
    assert manager.active_turn_ref == second.turn_ref
    await manager.abandon_turn(second.turn_ref, reason="test_cleanup")
    await manager.close()


@pytest.mark.asyncio
async def test_terminal_persistence_failure_unblocks_turn_and_retires_runtime():
    driver = _ControllableDriver()

    async def reject_terminal(event):
        event_type = getattr(event.type, "value", event.type)
        if event_type == "turn.completed":
            raise RuntimeError("event store unavailable")

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
        event_sink=reject_terminal,
    )
    running = asyncio.create_task(
        RuntimeManagerAdapter(manager).run_turn(_context())
    )
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef("native-session-1"),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))

    with pytest.raises(AgentAPIError, match="outcome abandoned"):
        await asyncio.wait_for(running, timeout=1)
    assert manager.active_turn_ref is None
    assert manager.opened is None


@pytest.mark.asyncio
async def test_failed_start_releases_the_event_subscriber():
    driver = _FailingStartDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )

    with pytest.raises(RuntimeError, match="provider refused the turn"):
        await asyncio.wait_for(
            RuntimeManagerAdapter(manager).run_turn(_context()), timeout=1
        )

    assert manager._subscribers == set()
    assert manager._terminal == {}
    assert manager.active_turn_ref is None
    await manager.close()


@pytest.mark.asyncio
async def test_unaccepted_receipt_releases_the_event_subscriber():
    driver = _RejectingStartDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )

    result = await asyncio.wait_for(
        RuntimeManagerAdapter(manager).run_turn(_context()), timeout=1
    )

    assert result.metadata == {"accepted": False}
    assert manager._subscribers == set()
    assert manager._terminal == {}
    assert manager.active_turn_ref is None
    await manager.close()

    ambiguous = _AmbiguousStartDriver()
    ambiguous_manager = RuntimeManager(
        ambiguous,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="claude-code",
    )
    result = await RuntimeManagerAdapter(ambiguous_manager).run_turn(_context())
    assert result.metadata == {"accepted": False}
    assert ambiguous.close_calls == 1
    assert ambiguous_manager.opened is None
    assert ambiguous_manager.native_session_id == ""
    await ambiguous_manager.close()


@pytest.mark.asyncio
async def test_silent_turn_start_is_bounded_by_the_task_timeout():
    driver = _PausedStartDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=0.05),
        driver_name="codex",
    )

    result = await asyncio.wait_for(
        RuntimeManagerAdapter(manager).run_turn(_context()), timeout=2
    )

    assert result.metadata["runtime_turn_timeout"] is True
    assert manager.active_turn_ref is None
    assert manager._subscribers == set()
    assert manager._terminal == {}
    assert driver.close_calls == 1
    assert manager.opened is None
    assert manager.native_session_id == ""
    driver.release_start.set()
    await manager.close()


@pytest.mark.asyncio
async def test_completed_terminals_are_pruned_and_released_on_close():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )

    first = await manager.start_turn(TurnInput("first"))
    await _complete_active_turn(manager, driver)
    # Production consumes terminals from events(), never wait_terminal, so the
    # settled future is still retained for a late waiter.
    assert list(manager._terminal) == [first.turn_ref]

    second = await manager.start_turn(TurnInput("second"))
    assert list(manager._terminal) == [second.turn_ref]

    waiting = asyncio.create_task(manager.wait_terminal(second.turn_ref))
    await asyncio.sleep(0)
    await manager.close()

    assert manager._terminal == {}
    with pytest.raises(RuntimeStateError, match="runtime is closed"):
        await asyncio.wait_for(waiting, timeout=1)


@pytest.mark.asyncio
async def test_compaction_completes_only_on_the_event_and_never_starts_twice():
    driver, manager, adapter = await _open_compacting_manager(wait_seconds=10)

    first = asyncio.create_task(adapter.compact_context())
    second = asyncio.create_task(adapter.compact_context())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    # Acceptance is not completion, and one decision starts one provider pass.
    assert driver.compact_calls == 1

    await _feed_compaction_completed(driver, manager)
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert [value.completed for value in results] == [True, True]
    assert driver.compact_calls == 1

    # An unobserved completion degrades gracefully after the bounded wait,
    # and leaves the outstanding operation alone rather than restarting it.
    adapter.compaction_wait_seconds = 0.01
    unobserved = await adapter.compact_context()
    assert not unobserved.completed
    assert "no completion event" in unobserved.diagnostic
    assert driver.compact_calls == 2
    assert (await adapter.compact_context()).completed is False
    assert driver.compact_calls == 2
    await manager.close()


@pytest.mark.asyncio
async def test_context_commands_are_locked_and_fail_closed_after_close():
    driver = _CompactingDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec(
            "/tmp",
            task_timeout_seconds=1,
            auto_compact_threshold_pct=50,
        ),
        driver_name="codex",
    )
    await manager.open()
    adapter = RuntimeManagerAdapter(manager, compaction_wait_seconds=10)

    active = await manager.start_turn(TurnInput("busy"))
    assert (await adapter.get_context_snapshot()).used_tokens == 12
    assert driver.open_calls == 1
    assert adapter.get_context_capabilities().native_compaction is False
    await manager.abandon_turn(active.turn_ref, reason="test_cleanup")

    assert (await adapter.get_context_snapshot()).used_tokens == 12
    assert driver.open_calls == 2
    assert manager.spec.auto_compact_threshold_tokens == 50

    waiting = asyncio.create_task(adapter.compact_context())
    await asyncio.sleep(0)
    assert driver.compact_calls == 1
    await manager.close()

    # The pending future is failed and retrieved, so no waiter hangs and no
    # "exception was never retrieved" warning escapes.
    assert manager._compaction is None
    assert (await asyncio.wait_for(waiting, timeout=1)).completed is False
    for command in (adapter.compact_context, adapter.get_context_snapshot):
        with pytest.raises(RuntimeStateError, match="runtime is closed"):
            await command()


@pytest.mark.asyncio
async def test_continuation_failure_persists_terminal_before_next_turn(tmp_path):
    driver = _ControllableDriver()
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    sink = RuntimeEventProjectingSink(
        outbox,
        RuntimeEventProjector(agent_id="agent", session_ref="session"),
    )
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
        event_sink=sink,
    )
    adapter = RuntimeManagerAdapter(manager)

    first = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    while not manager.native_turn_id:
        await asyncio.sleep(0)

    async def reject_admission(_event):
        raise RuntimeError("held admission failed")

    manager.register_continuation(
        reject_admission,
        "held-cycle",
        tool_names=("send_message",),
        tool_arguments={"channel": "ch-1"},
        correlation_receipt="receipt",
    )
    await driver.queue.put(HarnessEvent.normalized(
        type="turn.started",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        native_session_id=manager.native_session_id,
        native_turn_id=manager.native_turn_id,
    ))
    await driver.queue.put(HarnessEvent.normalized(
        type="turn.tool_completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        native_session_id=manager.native_session_id,
        native_turn_id=manager.native_turn_id,
        data={
            "tool_call_ref": "tool-1",
            "label": "send_message",
            "outcome": "succeeded",
        },
        native_payload={
            "_puffo_internal": "tool_result",
            "tool_call_id": "tool-1",
            "tool_name": "send_message",
            "arguments": {"channel": "ch-1"},
            "result_omitted": True,
            "is_error": False,
        },
    ))

    with pytest.raises(AgentAPIError, match="outcome abandoned"):
        await asyncio.wait_for(first, timeout=1)
    assert manager.active_turn_ref is None

    driver.started = asyncio.Event()
    second = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    while manager.native_turn_id != "native-turn-2":
        await asyncio.sleep(0)
    await driver.queue.put(HarnessEvent.normalized(
        type="turn.started",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        native_session_id=manager.native_session_id,
        native_turn_id=manager.native_turn_id,
    ))
    await driver.queue.put(HarnessEvent.normalized(
        type="turn.completed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        native_session_id=manager.native_session_id,
        native_turn_id=manager.native_turn_id,
        data={"outcome": "succeeded"},
    ))
    assert (await asyncio.wait_for(second, timeout=1)).reply == ""
    assert [row.event_type for row in outbox.prefix()] == [
        "turn.started",
        "activity.updated",
        "turn.finished",
        "turn.started",
        "activity.updated",
        "turn.finished",
    ]

    await adapter.aclose()
    outbox.close()
