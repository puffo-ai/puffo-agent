from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.errors import AgentAPIError, ProviderFailureError
from puffo_agent.agent.adapters.base import TurnContext, TurnResult
from puffo_agent.agent.harness.drivers.claude_code import (
    ClaudeCodeCliDriver,
    _provider_error,
)
from puffo_agent.agent.harness.support.cleanup_errors import cleanup_errors
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.driver import (
    CancelReceipt,
    CompactReceipt,
    ContextStatus,
    Driver,
    HarnessEvent,
    HarnessEventType,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnInput,
    TurnRef,
    TurnStarted,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
    RuntimeStateError,
)
from puffo_agent.agent.runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
)
from puffo_agent.agent.runtime_events import RuntimeEventProjector


@pytest.mark.asyncio
async def test_claude_usage_limit_is_a_terminal_failure_not_assistant_text():
    """Claude surfaces model quota exhaustion in an assistant-shaped frame."""
    driver = ClaudeCodeCliDriver()
    driver._session_ref = SessionRef("native")
    driver._native_session_id = "native-session"
    driver._active = TurnRef("turn-1")
    driver._active_native_turn_id = "native-turn"

    await driver._handle({
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "content": [{
                "type": "text",
                "text": (
                    "You've reached your Fable 5 limit. Switch to another "
                    "model or wait for your limit to reset."
                ),
            }],
        },
        "error": "rate_limit",
        "errorDetails": {"status": 429, "type": "rate_limit_error"},
        "isApiErrorMessage": True,
        "apiErrorStatus": 429,
    })

    assert driver._events.empty()
    assert driver._active == TurnRef("turn-1")
    with pytest.raises(RuntimeError, match="already active"):
        await driver.start_turn(TurnInput("retry"))

    # An empty bookkeeping frame is not evidence that the provider recovered.
    await driver._handle({
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": []},
    })
    await driver._handle({
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": 12, "output_tokens": 0},
    })

    events = []
    while not driver._events.empty():
        events.append(driver._events.get_nowait())
    event_types = [getattr(event.type, "value", event.type) for event in events]
    assert "turn.assistant_delta" not in event_types
    assert event_types == ["turn.completed"]
    assert events[0].data == {
        "outcome": "failed",
        "error_code": "quota_exhausted",
        "input_tokens": 12,
        "output_tokens": 0,
        "context_tokens": 12,
    }
    assert driver._active == TurnRef("")


def test_claude_403_uses_shared_auth_classification():
    permission = _provider_error({
        "type": "assistant",
        "isApiErrorMessage": True,
        "apiErrorStatus": 403,
        "errorDetails": {"type": "permission_error"},
    })
    explicit_auth = _provider_error({
        "type": "assistant",
        "isApiErrorMessage": True,
        "apiErrorStatus": 403,
        "errorDetails": {"type": "authentication_error"},
    })

    assert permission == {"error_code": "permission_denied"}
    assert explicit_auth == {"error_code": "authentication"}
    assert _provider_error({
        "type": "result",
        "isApiErrorMessage": True,
        "apiErrorStatus": 429,
    }) is None
    assert _provider_error({
        "type": "assistant",
        "parent_tool_use_id": "tool-child",
        "isApiErrorMessage": True,
        "apiErrorStatus": 429,
    }) is None


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


@pytest.mark.asyncio
async def test_native_resume_falls_back_on_invalid_resume_error_code():
    # laundered text: error_code alone must trigger the fallback
    driver = _ResumeBoundaryDriver(
        AgentAPIError(
            "The provider could not complete the turn.",
            is_auth=False,
            error_code="invalid_resume",
        )
    )
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
async def test_failed_turn_start_preserves_the_native_session():
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

    # retired but session kept; next open resumes the same thread
    assert manager.opened is None
    assert manager.native_session_id == "native-session-1"

    await manager.open()
    assert driver.open_calls == 2
    assert manager.native_session_id == "native-session-1"
    await manager.close()


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
@pytest.mark.parametrize(
    ("error_code", "expected_code", "expected_exception", "is_auth"),
    [
        ("authentication", "authentication", AgentAPIError, True),
        ("quota_exhausted", "quota_exhausted", ProviderFailureError, None),
        (None, "runtime_exited", AgentAPIError, False),
    ],
)
async def test_runtime_exit_preserves_provider_failure_and_allows_next_turn(
    error_code, expected_code, expected_exception, is_auth
):
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
        data={"error_code": error_code} if error_code else {},
    ))

    with pytest.raises(expected_exception) as excinfo:
        await asyncio.wait_for(first, timeout=1)
    if is_auth is not None:
        assert excinfo.value.is_auth is is_auth
    assert excinfo.value.error_code == expected_code
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

    with pytest.raises(AgentAPIError) as excinfo:
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
@pytest.mark.parametrize(
    ("error_code", "retryable", "is_auth"),
    [
        ("authentication", False, True),
        ("rate_limit", False, False),
    ],
)
async def test_known_provider_failures_share_recovery_semantics(
    error_code, retryable, is_auth
):
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="claude-code",
    )
    adapter = RuntimeManagerAdapter(manager)

    running = asyncio.create_task(adapter.run_turn(_context()))
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="claude-code",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={
            "outcome": "failed",
            "error_code": error_code,
            "retryable": retryable,
        },
    ))

    with pytest.raises(AgentAPIError) as raised:
        await asyncio.wait_for(running, timeout=1)
    assert raised.value.error_code == error_code
    assert raised.value.is_auth is is_auth
    await manager.close()


@pytest.mark.asyncio
async def test_retry_payload_depends_on_input_admission():
    async def open_():
        return None

    manager = SimpleNamespace(
        native_session_id="preserved-session",
        input_admitted=True,
        open=open_,
    )
    adapter = RuntimeManagerAdapter(manager)
    received: list[str] = []

    async def capture(ctx):
        received.append(ctx.messages[-1]["content"])
        return TurnResult(reply="")

    adapter.run_turn = capture
    context = TurnContext(system_prompt="system", messages=[])

    await adapter.run_retry_turn("continue", "full durable input", context)
    # survived but not admitted -> durable replay
    manager.input_admitted = False
    await adapter.run_retry_turn("continue", "full durable input", context)
    # no session -> durable replay
    manager.native_session_id = ""
    manager.input_admitted = True
    await adapter.run_retry_turn("continue", "full durable input", context)

    assert received == [
        "continue",
        "full durable input",
        "full durable input",
    ]


class _FailFirstStartDriver(_ControllableDriver):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[str] = []

    async def start_turn(self, input):
        if self.start_calls == 0:
            self.start_calls += 1
            raise RuntimeError("provider refused the turn")
        self.inputs.append(input.content)
        return await super().start_turn(input)


@pytest.mark.asyncio
async def test_failed_start_retry_replays_the_durable_payload():
    driver = _FailFirstStartDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )
    adapter = RuntimeManagerAdapter(manager)

    with pytest.raises(RuntimeError, match="provider refused the turn"):
        await asyncio.wait_for(adapter.run_turn(_context()), timeout=1)
    # session survives, admission cleared
    assert manager.native_session_id == "native-session-1"
    assert manager.input_admitted is False

    retry_ctx = TurnContext(
        system_prompt="system",
        messages=[{"role": "user", "content": "current notice"}],
    )
    running = asyncio.create_task(
        adapter.run_retry_turn("continue processing", "full durable input", retry_ctx)
    )
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await _complete_active_turn(manager, driver)
    await asyncio.wait_for(running, timeout=1)

    assert driver.inputs == ["full durable input"]
    assert manager.input_admitted is True
    await manager.close()


class _ResumeBoundaryTurnDriver(_ResumeBoundaryDriver):
    def __init__(self, resume_error: Exception) -> None:
        super().__init__(resume_error)
        self.inputs: list[str] = []

    async def start_turn(self, input):
        self.inputs.append(input.content)
        return await super().start_turn(input)


@pytest.mark.asyncio
async def test_retry_kick_is_reconsidered_after_a_fresh_session_fallback():
    # dead session: fresh fallback happens before the payload choice
    driver = _ResumeBoundaryTurnDriver(
        AgentAPIError(
            "The provider could not complete the turn.",
            is_auth=False,
            error_code="invalid_resume",
        )
    )
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
        driver_name="codex",
    )
    manager._input_admitted = True
    adapter = RuntimeManagerAdapter(manager)

    retry_ctx = TurnContext(
        system_prompt="system",
        messages=[{"role": "user", "content": "current notice"}],
    )
    running = asyncio.create_task(
        adapter.run_retry_turn("continue processing", "full durable input", retry_ctx)
    )
    await asyncio.wait_for(driver.started.wait(), timeout=1)
    await _complete_active_turn(manager, driver)
    await asyncio.wait_for(running, timeout=1)

    assert driver.resume_values == [SessionRef("native-old"), None]
    assert driver.inputs == ["full durable input"]
    await manager.close()


@pytest.mark.asyncio
async def test_unclassified_resume_failures_fall_back_after_a_bounded_streak():
    # unknown wording must not wedge forever
    driver = _ResumeBoundaryDriver(RuntimeError("Codex rollout missing"))
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="rollout missing"):
            await manager.open()
        assert manager.native_session_id == "native-old"

    opened = await manager.open()
    assert opened.session_ref == SessionRef("logical-session")
    assert manager.native_session_id == "native-session-1"
    assert driver.resume_values == [
        SessionRef("native-old"),
        SessionRef("native-old"),
        SessionRef("native-old"),
        None,
    ]
    await manager.close()


class _ResumeErrorSequenceDriver(_ControllableDriver):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = list(errors)
        self.resume_values: list[SessionRef | None] = []

    async def open(self, spec, resume=None):
        self.resume_values.append(resume)
        if resume is not None and self.errors:
            raise self.errors.pop(0)
        return await super().open(spec, resume)


@pytest.mark.asyncio
async def test_untagged_agent_api_errors_never_lose_the_session():
    # untagged AgentAPIError: recoverable, never feeds the streak
    driver = _ResumeBoundaryDriver(AgentAPIError("temporary transport failure"))
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    for _ in range(5):
        with pytest.raises(AgentAPIError, match="temporary transport failure"):
            await manager.open()
    assert manager.native_session_id == "native-old"
    assert all(v == SessionRef("native-old") for v in driver.resume_values)


@pytest.mark.asyncio
async def test_transient_resume_failures_reset_the_unclassified_streak():
    # transient resets the streak: fallback on the 6th attempt, not the 4th
    driver = _ResumeErrorSequenceDriver([
        RuntimeError("weird wording one"),
        RuntimeError("weird wording two"),
        AgentAPIError("rate limited", error_code="rate_limit"),
        RuntimeError("weird wording three"),
        RuntimeError("weird wording four"),
        RuntimeError("weird wording five"),
    ])
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    for match in (
        "wording one", "wording two", "rate limited",
        "wording three", "wording four",
    ):
        with pytest.raises(Exception, match=match):
            await manager.open()
        assert manager.native_session_id == "native-old"

    opened = await manager.open()
    assert opened.session_ref == SessionRef("logical-session")
    assert manager.native_session_id == "native-session-1"
    assert driver.resume_values[-1] is None
    await manager.close()


@pytest.mark.asyncio
async def test_categorized_provider_errors_hit_the_bounded_fallback():
    # provider_error (codex-laundered unknown wording) hits the fallback
    driver = _ResumeBoundaryDriver(
        ProviderFailureError(
            "The provider could not complete the turn.",
            error_code="provider_error",
        )
    )
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    for _ in range(2):
        with pytest.raises(ProviderFailureError):
            await manager.open()
        assert manager.native_session_id == "native-old"

    opened = await manager.open()
    assert opened.session_ref == SessionRef("logical-session")
    assert manager.native_session_id == "native-session-1"
    await manager.close()


@pytest.mark.asyncio
async def test_classified_transient_resume_failures_never_lose_the_session():
    driver = _ResumeBoundaryDriver(
        AgentAPIError("rate limited", is_auth=False, error_code="rate_limit")
    )
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )

    for _ in range(5):
        with pytest.raises(AgentAPIError, match="rate limited"):
            await manager.open()
    assert manager.native_session_id == "native-old"
    assert all(v == SessionRef("native-old") for v in driver.resume_values)


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
    assert result.reply == "Task produced no activity for 0.01 second and was stopped."
    assert driver.cancel_calls == 1
    assert driver.close_calls == 1
    assert manager.active_turn_ref is None
    assert manager.opened is None
    terminal = persisted[-1]
    assert str(terminal.type).endswith("TURN_ABANDONED")
    assert terminal.data["error_code"] == "turn_timeout"


@pytest.mark.asyncio
async def test_turn_timeout_extends_on_activity_and_never_fires():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=0.05),
        driver_name="claude-code",
    )
    running = asyncio.create_task(
        RuntimeManagerAdapter(manager).run_turn(_context())
    )
    await driver.started.wait()

    # Three gaps, each shorter than the timeout but summing to well over
    # it -- would time out if activity didn't keep pushing the deadline out.
    for _ in range(3):
        await asyncio.sleep(0.03)
        await driver.queue.put(HarnessEvent(
            type="turn.tool_started",
            driver="claude-code",
            session_ref=SessionRef(manager.native_session_id),
            turn_ref=driver.turn,
            data={},
        ))
    await driver.queue.put(HarnessEvent(
        type="turn.completed",
        driver="claude-code",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        data={"outcome": "succeeded"},
    ))

    result = await asyncio.wait_for(running, timeout=2)
    assert "runtime_turn_timeout" not in result.metadata
    await manager.close()


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

    with pytest.raises(AgentAPIError) as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.error_code == "runtime_exited"
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
async def test_cancel_during_timeout_still_publishes_and_retires_runtime():
    class CloseFailingPausedCancelDriver(_PausedCancelDriver):
        async def close(self):
            self.close_calls += 1
            raise RuntimeError("timeout retirement cleanup failed")

    driver = CloseFailingPausedCancelDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=1),
        driver_name="codex",
    )
    await manager.open()
    stream = manager.events()
    started = await manager.start_turn(TurnInput("first"))

    timeout = asyncio.create_task(manager.timeout_turn(started.turn_ref))
    await asyncio.wait_for(driver.cancel_entered.wait(), timeout=1)
    timeout.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await asyncio.wait_for(timeout, timeout=1)

    terminal = await asyncio.wait_for(anext(stream), timeout=1)
    assert terminal.type == HarnessEventType.TURN_ABANDONED
    assert terminal.data["error_code"] == "turn_timeout"
    assert [str(error) for error in cleanup_errors(exc_info.value)] == [
        "timeout retirement cleanup failed"
    ]
    assert manager.active_turn_ref is None
    assert manager.opened is None
    assert driver.close_calls == 1


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

    with pytest.raises(AgentAPIError) as excinfo:
        await asyncio.wait_for(running, timeout=1)
    assert excinfo.value.error_code == "event_persistence_failed"
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
    # failed start keeps the session
    assert ambiguous_manager.native_session_id == "native-session-1"
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
    assert manager.native_session_id == "native-session-1"
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
async def test_compaction_failed_event_fails_waiters_fast_with_the_diagnostic():
    """COMPACTION_FAILED must release waiters immediately, not by timeout.

    A provider 4xx/5xx during a driver-run compaction (e.g. OpenCode's
    summarize) emits the event; the manager fails its outstanding future so
    every coalesced caller returns with the diagnostic well inside the
    bounded wait, and a later compaction can start fresh.
    """
    driver, manager, adapter = await _open_compacting_manager(wait_seconds=10)

    first = asyncio.create_task(adapter.compact_context())
    second = asyncio.create_task(adapter.compact_context())
    await asyncio.sleep(0)
    assert driver.compact_calls == 1

    await driver.queue.put(HarnessEvent(
        type="compaction.failed",
        driver="codex",
        session_ref=SessionRef(manager.native_session_id),
        data={"diagnostic": "summarize returned HTTP 500: provider melted"},
    ))
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert [value.completed for value in results] == [False, False]
    assert all("HTTP 500" in value.diagnostic for value in results)

    # The failed operation is cleared: a retry issues a new provider pass.
    adapter.compaction_wait_seconds = 0.01
    await adapter.compact_context()
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


class _AutocompactEchoDriver(_ControllableDriver):
    """Reports back exactly the ceiling it was launched with.

    Models Claude Code's real behavior once --autocompact is active: its
    live context_window echoes the configured ceiling rather than the
    model's raw capacity.
    """

    async def context_status(self):
        return ContextStatus(used_tokens=1, context_window=300_000)


@pytest.mark.asyncio
async def test_context_snapshot_stays_pinned_once_it_matches_launch(monkeypatch):
    # equation-7256-87f7's real failure: launched with threshold=300_000
    # (opus-4-7's 1_000_000 static window * 30%, per PR #219's
    # claude_autocompact_tokens()). Claude Code then echoes context_window
    # back as 300_000 too -- re-deriving `window * pct / 100` from *that*
    # on every poll shrank the threshold each time (300k -> 90k -> ...)
    # until the CLI rejected the value outright. The launch spec must stay
    # authoritative across later observations.
    # Docker launch intentionally ignores the daemon's host-level window.
    # A later telemetry poll must not replace that launch-owned decision by
    # consulting a different environment.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    driver = _AutocompactEchoDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec(
            "/tmp",
            model="opus-4-7",
            auto_compact_threshold_pct=30,
            auto_compact_threshold_tokens=300_000,
        ),
        driver_name="claude-code",
    )
    await manager.open()
    adapter = RuntimeManagerAdapter(manager, compaction_wait_seconds=10)

    for _ in range(3):
        await adapter.get_context_snapshot()
        assert manager.spec.auto_compact_threshold_tokens == 300_000
    metadata = {}
    await adapter._refresh_terminal_context(metadata)
    assert adapter.context_limits() == (300_000, 300_000)
    assert driver.open_calls == 1


@pytest.mark.asyncio
async def test_context_snapshot_corrects_a_stale_claude_threshold_exactly_once(
    monkeypatch,
):
    # pct is configured but spec has no token value yet -- resolve once,
    # from the spec environment rather than the daemon environment, reload,
    # then agree on every later poll.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    driver = _AutocompactEchoDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", model="opus-4-7", auto_compact_threshold_pct=30),
        driver_name="claude-code",
    )
    await manager.open()
    adapter = RuntimeManagerAdapter(manager, compaction_wait_seconds=10)

    await adapter.get_context_snapshot()
    assert manager.spec.auto_compact_threshold_tokens == 300_000
    assert driver.open_calls == 2

    await adapter.get_context_snapshot()
    assert driver.open_calls == 2


@pytest.mark.asyncio
async def test_context_rollover_preserves_logical_session_and_opens_fresh_native():
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        session_ref=SessionRef("logical-session"),
        native_session_id="native-old",
    )
    adapter = RuntimeManagerAdapter(manager)
    await manager.open()

    result = await adapter.rollover_context()

    assert result.completed is True
    assert result.previous_provider_session_id == "native-old"
    assert result.provider_session_id == "native-session-2"
    assert manager.session_ref == SessionRef("logical-session")
    assert manager.opened is not None and manager.opened.resumed is False
    assert adapter.get_context_capabilities().rollover is True
    await manager.close()


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

    with pytest.raises(AgentAPIError) as excinfo:
        await asyncio.wait_for(first, timeout=1)
    assert excinfo.value.error_code == "runtime_event_processing_failed"
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


def _autonomous_manager(driver_name="claude-code", *, event_sink=None):
    return RuntimeManager(
        driver=_ControllableDriver(),
        spec=RuntimeSpec("/tmp", task_timeout_seconds=5),
        driver_name=driver_name,
        event_sink=event_sink,
    )


async def _start_autonomous(manager, reported):
    async def on_autonomous(event):
        reported.append((
            getattr(event.type, "value", event.type), dict(event.data),
        ))

    manager.autonomous_callback = on_autonomous
    await manager._consume_event_locked(HarnessEvent(
        type="turn.autonomous_started",
        driver=manager.driver_name,
        session_ref=SessionRef("native"),
        turn_ref=TurnRef("driver-turn"),
        native_turn_id="native-turn-1",
    ))
    assert manager.active_turn_ref is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["runtime_exited", "stream_ended", "closed"])
async def test_every_terminal_path_releases_an_adopted_autonomous_turn(ending):
    """A background run must end exactly once however the runtime goes away.
    Without this the daemon holds its durable turn in_turn past a refresh or
    an ordinary shutdown, blocking later turns until the process restarts."""
    manager = _autonomous_manager()
    reported: list[tuple[str, dict]] = []
    await _start_autonomous(manager, reported)

    if ending == "runtime_exited":
        await manager._consume_event_locked(HarnessEvent(
            type="runtime.exited",
            driver="claude-code",
            session_ref=SessionRef("native"),
        ))
    elif ending == "stream_ended":
        await manager._fail_runtime_locked("event_stream_ended")
    else:
        await manager.close()

    terminals = [kind for kind, _ in reported[1:]]
    assert len(terminals) == 1, reported
    assert reported[-1][1]["outcome"] == "abandoned"
    assert manager.active_turn_ref is None
    assert manager._autonomous_turn is None


@pytest.mark.asyncio
async def test_autonomous_terminal_follows_persistence_conversion():
    """When persisting the terminal fails the manager converts it to an
    abandon. The daemon must settle on that same conversion -- reporting the
    raw success would mark Inbox rows processed for a turn everyone else
    treats as abandoned."""
    async def failing_sink(event):
        if getattr(event.type, "value", event.type) == "turn.autonomous_completed":
            raise RuntimeError("event store unavailable")

    manager = _autonomous_manager(event_sink=failing_sink)
    reported: list[tuple[str, dict]] = []
    await _start_autonomous(manager, reported)

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await manager._consume_event_locked(HarnessEvent(
            type="turn.autonomous_completed",
            driver="claude-code",
            session_ref=SessionRef("native"),
            turn_ref=TurnRef("driver-turn"),
            native_turn_id="native-turn-1",
            data={"outcome": "succeeded"},
        ))

    # Exactly one terminal, and it is the converted one. Announcing the raw
    # success first would have let the daemon mark rows processed before the
    # abandon arrived -- too late to requeue them.
    terminals = [kind for kind, _ in reported[1:]]
    assert len(terminals) == 1, reported
    assert reported[-1][1]["outcome"] == "abandoned"
    assert reported[-1][1]["error_code"] == "event_persistence_failed"
    assert manager.active_turn_ref is None


@pytest.mark.asyncio
async def test_autonomous_turn_supports_held_send_admission():
    """The point of adopting an autonomous run is that platform behaviour
    keeps working inside it. Held-send admission needs the manager to have a
    real active turn and native turn id, so registering a continuation during
    an autonomous run must succeed rather than raise."""
    driver = _ControllableDriver()
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp", task_timeout_seconds=5),
        driver_name="claude-code",
    )
    adapter = RuntimeManagerAdapter(manager)

    # No daemon turn: exactly the state a background wakeup runs in.
    assert manager.active_turn_ref is None
    with pytest.raises(RuntimeStateError, match="no active provider turn"):
        manager.register_continuation(lambda _event: None, "cycle")

    await manager._consume_event_locked(HarnessEvent(
        type="turn.autonomous_started",
        driver="claude-code",
        session_ref=SessionRef("native"),
        turn_ref=TurnRef("driver-turn"),
        native_turn_id="native-turn-1",
    ))
    assert manager.active_turn_ref is not None
    assert manager.native_turn_id == "native-turn-1"

    # This is the call that used to raise, taking held-send recovery with it.
    manager.register_continuation(lambda _event: None, "cycle")
    assert manager._continuation_admissions

    await manager._consume_event_locked(HarnessEvent(
        type="turn.autonomous_completed",
        driver="claude-code",
        session_ref=SessionRef("native"),
        turn_ref=TurnRef("driver-turn"),
        native_turn_id="native-turn-1",
        data={"outcome": "succeeded"},
    ))
    # The terminal runs the same cleanup a normal turn does.
    assert manager.active_turn_ref is None
    assert manager.native_turn_id == ""
    assert not manager._continuation_admissions
    del adapter
