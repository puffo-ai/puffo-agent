from __future__ import annotations

import pytest

from puffo_agent.agent.harness.drivers.claude_code import ClaudeCodeCliDriver
from puffo_agent.agent.harness.driver import SessionRef, TurnInput

@pytest.mark.asyncio
async def test_autonomous_run_projects_a_full_turn_lifecycle():
    """A harness background task waking the model must produce a real turn,
    tool lifecycle included: held-send admission correlates on TOOL_COMPLETED
    and on the manager's active turn, so a bare start/end pair is unusable."""
    driver = ClaudeCodeCliDriver()
    driver._session_ref = SessionRef("native")
    driver._native_session_id = "native-session"
    assert driver._active.value == ""

    await driver._handle({"type": "assistant", "uuid": "auto-1", "message": {
        "content": [
            {"type": "text", "text": "background task finished"},
            {"type": "tool_use", "id": "tool-1", "name": "send_message",
             "input": {"channel": "ch_a"}},
        ],
    }})
    autonomous_turn = driver._active
    assert autonomous_turn.value  # a real turn, not an unbound run
    await driver._handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "held"},
    ]}})
    await driver._handle({"type": "result", "subtype": "success", "message": {}})

    events = []
    while not driver._events.empty():
        events.append(driver._events.get_nowait())
    kinds = [getattr(event.type, "value", event.type) for event in events]
    assert kinds[0] == "turn.autonomous_started"
    assert "turn.tool_started" in kinds
    assert "turn.tool_completed" in kinds
    assert kinds[-1] == "turn.autonomous_completed"
    # Every event carries the turn, which is what correlation keys off.
    assert all(
        event.turn_ref == autonomous_turn for event in events
    )
    assert driver._active.value == ""


@pytest.mark.asyncio
async def test_daemon_turn_is_refused_while_an_autonomous_run_is_active():
    """The provider runs one turn at a time. Starting a daemon turn during an
    autonomous run would let one turn's terminal end the other."""
    driver = ClaudeCodeCliDriver()
    driver._session_ref = SessionRef("native")
    driver._native_session_id = "native-session"
    driver._write = _noop_write  # type: ignore[method-assign]

    await driver._handle({"type": "assistant", "message": {"content": []}})
    assert driver._autonomous is True

    with pytest.raises(RuntimeError, match="already active"):
        await driver.start_turn(TurnInput(content="daemon turn"))

    # Once the autonomous run ends the session is free again.
    await driver._handle({"type": "result", "subtype": "success", "message": {}})
    assert driver._autonomous is False
    started = await driver.start_turn(TurnInput(content="daemon turn"))
    assert started.accepted is True


async def _noop_write(frame):
    del frame
