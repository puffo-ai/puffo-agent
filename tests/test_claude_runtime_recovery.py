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


@pytest.mark.asyncio
async def test_invalid_resume_result_has_stable_error_code():
    driver = ClaudeCodeCliDriver()
    driver._session_ref = SessionRef("native")
    driver._native_session_id = "missing-session"
    driver._active = TurnRef("turn")

    await driver._handle_result({
        "type": "result",
        "subtype": "error_during_execution",
        "errors": [
            "No conversation found with session ID: missing-session"
        ],
    }, "error_during_execution")

    completed = await driver._events.get()
    assert completed.data["error_code"] == "invalid_resume"
