from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from puffo_agent.agent.harness.drivers.claude_code import ClaudeCodeCliDriver
from puffo_agent.agent.harness.driver import RuntimeSpec, SessionRef, TurnRef
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
)


class _ContextStdin:
    def __init__(self, driver: ClaudeCodeCliDriver) -> None:
        self.driver = driver

    def write(self, value: bytes) -> None:
        frame = json.loads(value)
        request_id = frame["request_id"]
        self.driver._context_requests[request_id].set_result({
            "totalTokens": 495_678,
            "rawMaxTokens": 1_000_000,
            "autoCompactThreshold": 967_000,
            "isAutoCompactEnabled": True,
        })

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_context_query_discovers_pre_turn_compaction_capability():
    driver = ClaudeCodeCliDriver()
    driver._proc = SimpleNamespace(stdin=_ContextStdin(driver))
    manager = RuntimeManager(driver, RuntimeSpec("/workspace"))
    adapter = RuntimeManagerAdapter(manager)

    assert adapter.get_context_capabilities().native_compaction is False
    status = await manager.context_status()

    assert status.auto_compact_threshold_tokens == 967_000
    assert status.auto_compact_enabled is True
    assert adapter.get_context_capabilities().native_compaction is True
    assert (await adapter.get_context_snapshot()).used_tokens == 495_678
    assert adapter.context_limits() == (1_000_000, 967_000)


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.asyncio
async def test_invalid_resume_result_has_stable_error_code(is_error):
    driver = ClaudeCodeCliDriver()
    driver._session_ref = SessionRef("native")
    driver._native_session_id = "missing-session"
    driver._active = TurnRef("turn")

    await driver._handle_result({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": is_error,
        "errors": [
            "No conversation found with session ID: missing-session"
        ],
    }, "error_during_execution")

    completed = await driver._events.get()
    assert completed.data["error_code"] == "invalid_resume"


@pytest.mark.asyncio
async def test_completed_assistant_replay_cannot_arm_watchdog(monkeypatch, tmp_path):
    """Replaying a completed raw frame must not quarantine an idle session."""
    import asyncio

    from test_runtime_manager_failures import _ControllableDriver
    from puffo_agent.agent.turn_recovery import read_recovery

    loop = asyncio.get_running_loop()
    now = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: now[0])
    decoder = ClaudeCodeCliDriver()
    decoder._session_ref = SessionRef("native-session")
    decoder._native_session_id = "native-session"
    manager = RuntimeManager(
        _ControllableDriver(), RuntimeSpec(str(tmp_path), task_timeout_seconds=5),
        native_session_id="native-session",
    )
    reported = []

    async def callback(event):
        reported.append(event)

    manager.autonomous_callback = callback

    async def feed(frame):
        await decoder._handle(frame)
        while not decoder._events.empty():
            await manager._consume_event_locked(decoder._events.get_nowait())

    assistant = {
        "type": "assistant", "uuid": "completed-assistant",
        "message": {"content": [{"type": "text", "text": "done"}]},
    }
    result = {"type": "result", "subtype": "success", "is_error": False}
    try:
        await feed(assistant)
        await feed(result)
        completed_events = len(reported)
        await feed(assistant)
        await asyncio.sleep(0)
        now[0] += 6
        for _ in range(12):
            await asyncio.sleep(0)
        assert manager.driver.close_calls == 0
        assert read_recovery(str(tmp_path)) is None
        assert manager.active_turn_ref is None
        assert len(reported) == completed_events
        # Genuine background work must still be adopted and completed.
        await feed({**assistant, "uuid": "new-assistant"})
        assert manager.active_turn_ref is not None
        await feed(result)
        assert manager.active_turn_ref is None
    finally:
        await manager.close()
