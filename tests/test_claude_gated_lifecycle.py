from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.agent.harness.claude_code_driver import (
    ClaudeCodeCliDriver,
    claude_capabilities,
)
from puffo_agent.agent.harness.driver import RuntimeSpec, TurnInput
from puffo_agent.agent.harness.runtime_manager import RuntimeManager


class _Stdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        json.loads(value)
        self.writes.append(value)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _Process:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None

    def feed(self, frame: dict) -> None:
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()

    kill = terminate

    async def wait(self) -> int:
        return int(self.returncode or 0)


def _lifecycle(proc: _Process, command_id: str, state: str) -> None:
    proc.feed({
        "type": "command_lifecycle",
        "command_uuid": command_id,
        "state": state,
    })


def _result(proc: _Process, command_id: str, input_: int, output: int) -> None:
    proc.feed({
        "type": "result",
        "subtype": "success",
        "user_message_uuid": command_id,
        "usage": {"input_tokens": input_, "output_tokens": output},
    })


async def _next(stream, event_type: str):
    async for event in stream:
        if getattr(event.type, "value", event.type) == event_type:
            return event
    raise AssertionError(f"event stream ended before {event_type}")


async def _open_driver(*, ack_timeout: float = 0.2):
    proc = _Process()
    proc.feed({
        "type": "system",
        "subtype": "init",
        "session_id": "claude-lifecycle",
        "capabilities": ["msg_lifecycle_v1"],
    })
    driver = ClaudeCodeCliDriver(
        lambda _args, _spec: proc,
        input_ack_timeout=ack_timeout,
    )
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    await _next(stream, "session.opened")
    started = await driver.start_turn(
        TurnInput("first", client_correlation_id="primary")
    )
    _lifecycle(proc, "primary", "queued")
    _lifecycle(proc, "primary", "started")
    proc.feed({
        "type": "user",
        "session_id": "claude-lifecycle",
        "parent_tool_use_id": None,
        "uuid": "primary",
        "isReplay": True,
        "message": {"role": "user", "content": "first"},
    })
    assert (await _next(stream, "turn.started")).native_turn_id == "primary"
    return proc, driver, stream, started


async def _offer_gated(proc, driver, started):
    pending = asyncio.create_task(driver.steer_turn(
        started.turn_ref,
        TurnInput("new inbox", client_correlation_id="gated"),
    ))
    while len(proc.stdin.writes) < 2:
        await asyncio.sleep(0)
    _lifecycle(proc, "gated", "queued")
    return await pending


def test_claude_lifecycle_capability_is_explicit():
    assert claude_capabilities().steer == "none"
    assert claude_capabilities(message_lifecycle_v1=True).steer == "gated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery_shape",
    ["folded", "separate", "discarded", "unknown"],
)
async def test_gated_commands_share_one_logical_turn(delivery_shape):
    proc, driver, stream, started = await _open_driver()
    receipt = await _offer_gated(proc, driver, started)
    assert receipt.accepted and receipt.delivery == "queued_native_command"
    assert not (await driver.steer_turn(started.turn_ref, TurnInput("third"))).accepted

    if delivery_shape == "folded":
        _lifecycle(proc, "gated", "started")
        _lifecycle(proc, "gated", "completed")
        _result(proc, "primary", 10, 1)
        _lifecycle(proc, "primary", "completed")
        expected = (10, 1, 10)
        expected_error = ""
    elif delivery_shape == "separate":
        _result(proc, "primary", 10, 1)
        _lifecycle(proc, "primary", "completed")
        _lifecycle(proc, "gated", "started")
        _result(proc, "gated", 20, 2)
        _lifecycle(proc, "gated", "completed")
        expected = (30, 3, 20)
        expected_error = ""
    elif delivery_shape == "discarded":
        _result(proc, "primary", 10, 1)
        _lifecycle(proc, "primary", "completed")
        _lifecycle(proc, "gated", delivery_shape)
        expected = (10, 1, 10)
        expected_error = f"command_{delivery_shape}"
    else:
        _result(proc, "primary", 10, 1)
        _lifecycle(proc, "primary", "completed")
        _lifecycle(proc, "gated", "future_state")
        expected = (10, 1, 10)
        expected_error = "command_lifecycle_protocol"

    completed = await _next(stream, "turn.completed")
    assert completed.native_turn_id == "primary"
    assert tuple(completed.data[key] for key in (
        "input_tokens", "output_tokens", "context_tokens"
    )) == expected
    assert completed.data.get("error_code", "") == expected_error
    await driver.close()


@pytest.mark.asyncio
async def test_gated_command_without_queue_ack_is_not_accepted():
    proc, driver, stream, started = await _open_driver(ack_timeout=0.01)
    receipt = await driver.steer_turn(started.turn_ref, TurnInput("new inbox"))
    assert not receipt.accepted and receipt.delivery == "queue_ack_timeout"
    assert not receipt.session_reusable
    _result(proc, "orphaned-gated-command", 99, 99)
    _result(proc, "primary", 0, 0)
    _lifecycle(proc, "primary", "completed")
    completed = await _next(stream, "turn.completed")
    assert completed.data["input_tokens"] == 0
    await driver.close()


@pytest.mark.asyncio
async def test_process_exit_makes_pending_input_admission_ambiguous():
    proc, driver, _stream, started = await _open_driver()
    pending = asyncio.create_task(driver.steer_turn(
        started.turn_ref,
        TurnInput("new inbox", client_correlation_id="gated"),
    ))
    while len(proc.stdin.writes) < 2:
        await asyncio.sleep(0)
    proc.stdout.feed_eof()

    receipt = await pending
    assert not receipt.accepted and not receipt.session_reusable
    assert receipt.delivery == "queue_ack_ambiguous"
    await driver.close()


@pytest.mark.asyncio
async def test_gated_command_rejected_before_queue_keeps_session_usable():
    proc, driver, stream, started = await _open_driver()
    pending = asyncio.create_task(driver.steer_turn(
        started.turn_ref,
        TurnInput("new inbox", client_correlation_id="gated"),
    ))
    while len(proc.stdin.writes) < 2:
        await asyncio.sleep(0)
    _lifecycle(proc, "gated", "discarded")

    receipt = await pending
    assert not receipt.accepted and receipt.session_reusable
    assert receipt.delivery == "native_command_rejected"
    _result(proc, "primary", 10, 1)
    _lifecycle(proc, "primary", "completed")
    completed = await _next(stream, "turn.completed")
    assert completed.data["outcome"] == "succeeded"
    await driver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [["msg_lifecycle_v2"], ["msg_lifecycle_v1", "msg_lifecycle_v2"]],
)
async def test_unknown_lifecycle_dialect_does_not_enable_gated_delivery(
    capabilities,
):
    proc = _Process()
    proc.feed({
        "type": "system",
        "subtype": "init",
        "session_id": "claude-v2",
        "capabilities": capabilities,
    })
    driver = ClaudeCodeCliDriver(lambda _args, _spec: proc)
    await driver.open(RuntimeSpec("/workspace"))
    await _next(driver.events(), "session.opened")
    assert driver.current_capabilities().steer == "none"
    await driver.close()


@pytest.mark.asyncio
async def test_ambiguous_gated_receipt_retires_native_session():
    proc = _Process()
    proc.feed({
        "type": "system",
        "subtype": "init",
        "session_id": "ambiguous-session",
        "capabilities": ["msg_lifecycle_v1"],
    })
    driver = ClaudeCodeCliDriver(
        lambda _args, _spec: proc,
        input_ack_timeout=0.01,
    )
    manager = RuntimeManager(driver, RuntimeSpec("/workspace"))
    await manager.open()
    started = await manager.start_turn(
        TurnInput("first", client_correlation_id="primary")
    )

    receipt = await manager.steer_turn(started.turn_ref, TurnInput("new inbox"))

    assert not receipt.accepted and not receipt.session_reusable
    assert manager.active_turn_ref is None
    assert manager.opened is None
    assert manager.native_session_id == ""
    assert proc.returncode == 0
    await manager.close()
