import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.harness.support.cleanup_errors import (
    CleanupTimeoutError,
    cleanup_errors,
    collect_cleanup_errors,
    raise_collected_errors,
    suppressed_primary_errors,
)
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.driver import (
    Driver,
    HarnessEvent,
    HarnessEventType,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnRef,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
)


class _CloseErrorDriver(Driver):
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.close_calls = 0

    async def open(self, spec, resume=None):
        return RuntimeOpened(
            RuntimeRef("runtime"),
            SessionRef("native-session"),
            "native-session",
            False,
            CODEX_CAPABILITIES,
            SimpleNamespace(),
        )

    async def start_turn(self, input):
        return UnsupportedCapability("start_turn")

    async def steer_turn(self, turn, input):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn):
        return UnsupportedCapability("cancel")

    async def context_status(self):
        return UnsupportedCapability("context_status")

    async def compact(self, request):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request, decision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                yield await self.queue.get()

        return iterate()

    async def close(self):
        self.close_calls += 1
        raise RuntimeError("process-tree cleanup failed")


def test_cleanup_reader_rejects_missing_protocol_marker():
    with pytest.raises(LookupError, match="no structured cleanup evidence"):
        cleanup_errors(asyncio.CancelledError())


def test_cleanup_reader_distinguishes_checked_clean_from_missing_protocol():
    failure = RuntimeError("primary")
    with pytest.raises(RuntimeError) as exc_info:
        raise_collected_errors("single failure", [failure])

    assert cleanup_errors(exc_info.value) == ()


def test_cleanup_reader_rejects_malformed_protocol_marker():
    failure = RuntimeError("primary")
    failure._puffo_cleanup_errors = "not-a-tuple"  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="malformed structured cleanup"):
        cleanup_errors(failure)


def test_cancellation_separates_suppressed_primary_from_cleanup_failure():
    primary = ValueError("business failure")
    cancellation = asyncio.CancelledError()
    cleanup = RuntimeError("cleanup failure")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        raise_collected_errors(
            "cancelled cleanup", [primary, cancellation, cleanup]
        )

    assert suppressed_primary_errors(exc_info.value) == (primary,)
    assert cleanup_errors(exc_info.value) == (cleanup,)
    assert any(
        "primary failure suppressed by cancellation" in note
        for note in exc_info.value.__notes__
    )


@pytest.mark.asyncio
async def test_cleanup_supervision_has_an_explicit_upper_bound():
    release = asyncio.Event()

    async def resist_cancellation_once():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()

    errors: list[BaseException] = []
    await asyncio.wait_for(
        collect_cleanup_errors(
            resist_cancellation_once(), errors, timeout=0.01
        ),
        timeout=0.2,
    )

    assert len(errors) == 1
    assert isinstance(errors[0], CleanupTimeoutError)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cleanup_failure_during_timeout_cancellation_is_kept():
    async def fail_during_cancellation():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed during cancellation")

    errors: list[BaseException] = []
    await collect_cleanup_errors(
        fail_during_cancellation(), errors, timeout=0.01
    )

    assert len(errors) == 2
    assert isinstance(errors[0], CleanupTimeoutError)
    assert isinstance(errors[1], RuntimeError)
    assert str(errors[1]) == "cleanup failed during cancellation"


@pytest.mark.asyncio
async def test_caller_cancellation_during_timeout_settlement_stays_distinct():
    collector: asyncio.Task[None]

    async def fail_and_cancel_caller():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            collector.cancel("caller cancellation")
            raise RuntimeError("cleanup failed during cancellation")

    errors: list[BaseException] = []
    collector = asyncio.create_task(
        collect_cleanup_errors(
            fail_and_cancel_caller(), errors, timeout=0.01
        )
    )
    await collector

    assert len(errors) == 3
    assert isinstance(errors[0], asyncio.CancelledError)
    assert isinstance(errors[1], CleanupTimeoutError)
    assert isinstance(errors[2], RuntimeError)

    with pytest.raises(asyncio.CancelledError) as exc_info:
        raise_collected_errors("cancelled cleanup", errors)

    assert exc_info.value is errors[0]
    assert cleanup_errors(exc_info.value) == (errors[1], errors[2])


@pytest.mark.asyncio
async def test_cleanup_completion_failure_is_kept_during_caller_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_after_release():
        entered.set()
        await release.wait()
        raise RuntimeError("cleanup boom")

    errors: list[BaseException] = []
    collector = asyncio.create_task(
        collect_cleanup_errors(
            fail_after_release(), errors, timeout=1.0
        )
    )
    await entered.wait()

    release.set()
    collector.cancel()
    await collector

    with pytest.raises(asyncio.CancelledError) as exc_info:
        raise_collected_errors("cancelled cleanup", errors)

    assert len(errors) == 2
    assert isinstance(errors[0], asyncio.CancelledError)
    assert isinstance(errors[1], RuntimeError)
    assert str(errors[1]) == "cleanup boom"
    assert cleanup_errors(exc_info.value) == (errors[1],)


@pytest.mark.asyncio
async def test_inner_cleanup_timeout_propagates_through_outer_collector():
    async def nested_cleanup():
        inner_errors: list[BaseException] = []
        await collect_cleanup_errors(
            asyncio.Future(), inner_errors, timeout=0.01
        )
        raise_collected_errors("inner cleanup", inner_errors)

    errors: list[BaseException] = []
    await collect_cleanup_errors(nested_cleanup(), errors, timeout=1.0)

    assert len(errors) == 1
    assert isinstance(errors[0], CleanupTimeoutError)
    assert str(errors[0]) == "cleanup exceeded 0.01 seconds"


@pytest.mark.asyncio
async def test_cleanup_timeout_error_is_not_rewritten_as_collector_timeout():
    original = TimeoutError("operation timed out")

    async def fail():
        raise original

    errors: list[BaseException] = []
    await collect_cleanup_errors(fail(), errors, timeout=1.0)

    assert errors == [original]


@pytest.mark.asyncio
async def test_adapter_post_close_hook_cannot_block_close_forever(monkeypatch):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.runtime.runtime_manager.CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    release = asyncio.Event()

    async def hanging_post_close():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()

    class ImmediateManager:
        async def close(self):
            return None

    # Keep the assertion focused on the caller-provided hook; a real manager
    # has its own separately tested bounded cleanup operations.
    adapter = RuntimeManagerAdapter(  # type: ignore[arg-type]
        ImmediateManager(), post_close=hanging_post_close
    )

    with pytest.raises(CleanupTimeoutError, match="exceeded 0.01 seconds"):
        await asyncio.wait_for(adapter.aclose(), timeout=0.2)

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_manager_close_finishes_state_cleanup_before_propagating_error():
    driver = _CloseErrorDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"))
    await manager.open()
    stream = manager.events()

    with pytest.raises(RuntimeError, match="process-tree cleanup failed"):
        await manager.close()

    assert manager.opened is None
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_runtime_exit_preserves_primary_and_cleanup_failures():
    driver = _CloseErrorDriver()

    async def failing_sink(_event):
        raise ValueError("primary persistence failure")

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        event_sink=failing_sink,
    )
    await manager.open()

    with pytest.raises(ExceptionGroup) as exc_info:
        await manager._handle_runtime_exit_locked(HarnessEvent(
            type=HarnessEventType.RUNTIME_EXITED,
            driver="fake",
            session_ref=manager.session_ref,
            data={"returncode": 1},
        ))

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "primary persistence failure",
        "process-tree cleanup failed",
    ]
    assert manager.opened is None
    await manager._stop_reader()


@pytest.mark.asyncio
async def test_runtime_exit_cancellation_still_closes_and_preserves_both():
    driver = _CloseErrorDriver()

    async def cancelled_sink(_event):
        raise asyncio.CancelledError

    manager = RuntimeManager(
        driver,
        RuntimeSpec("/tmp"),
        event_sink=cancelled_sink,
    )
    await manager.open()

    task = asyncio.create_task(
        manager._handle_runtime_exit_locked(HarnessEvent(
            type=HarnessEventType.RUNTIME_EXITED,
            driver="fake",
            session_ref=manager.session_ref,
            data={"returncode": 1},
        ))
    )
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    assert [str(exc) for exc in cleanup_errors(exc_info.value)] == [
        "process-tree cleanup failed"
    ]
    assert driver.close_calls == 1
    assert manager.opened is None
    await manager._stop_reader()


@pytest.mark.asyncio
async def test_invalid_resume_cancellation_still_closes_and_preserves_both(
    monkeypatch,
):
    driver = _CloseErrorDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"))
    await manager.open()

    async def cancelled_terminal(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        manager,
        "_publish_terminal_locked",
        cancelled_terminal,
    )

    task = asyncio.create_task(
        manager._retire_invalid_resume_locked(
            HarnessEvent(
                type=HarnessEventType.TURN_ABANDONED,
                driver="fake",
                session_ref=manager.session_ref,
            ),
            TurnRef("turn_cancelled"),
        )
    )
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    assert [str(exc) for exc in cleanup_errors(exc_info.value)] == [
        "process-tree cleanup failed"
    ]
    assert driver.close_calls == 1
    assert manager.opened is None
    await manager._stop_reader()


@pytest.mark.asyncio
async def test_open_and_close_failures_are_grouped_without_fresh_fallback():
    class OpenAndCloseErrorDriver(_CloseErrorDriver):
        def __init__(self):
            super().__init__()
            self.open_calls = 0

        async def open(self, spec, resume=None):
            self.open_calls += 1
            raise ValueError("primary open failure")

    driver = OpenAndCloseErrorDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"))
    manager.native_session_id = "resume-me"

    with pytest.raises(ExceptionGroup) as exc_info:
        await manager.open()

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "primary open failure",
        "process-tree cleanup failed",
    ]
    assert driver.open_calls == 1
    assert driver.close_calls == 1


@pytest.mark.asyncio
async def test_start_and_retire_failures_are_grouped():
    class StartAndCloseErrorDriver(_CloseErrorDriver):
        async def start_turn(self, input):
            raise ValueError("primary start failure")

    driver = StartAndCloseErrorDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"))
    await manager.open()

    with pytest.raises(ExceptionGroup) as exc_info:
        await manager.start_turn(SimpleNamespace(content="hello"))

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "primary start failure",
        "process-tree cleanup failed",
    ]
    assert driver.close_calls == 1
    assert manager.opened is None


@pytest.mark.asyncio
async def test_adapter_close_aggregates_manager_and_post_close_failures():
    class FailingManager:
        async def close(self):
            raise ValueError("manager cleanup failed")

    async def failing_post_close():
        raise RuntimeError("container cleanup failed")

    adapter = RuntimeManagerAdapter(
        FailingManager(),
        post_close=failing_post_close,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await adapter.aclose()

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "manager cleanup failed",
        "container cleanup failed",
    ]
