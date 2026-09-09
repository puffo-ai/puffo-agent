"""Measure what Puffo does when the ACP committed-fact receipt never arrives.

Question under test (Boris, for the LingTai admission-loss A/B decision):
when the reliable receipt is lost, does the argument-correlation fallback in
``_admit_matching_tool_result`` still release the pending continuation?
"""
from __future__ import annotations

import hashlib
import pytest

from puffo_agent.agent.harness.driver import HarnessEvent, SessionRef, TurnInput
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeSpec,
)
from test_runtime_manager_failures import _ControllableDriver


def _acp_fact(manager, driver, *, call_id, receipt, committed, tool_name="read_inbox"):
    """An ACP-shaped tool_result fact -- exactly the keys acp.py emits."""
    native = {
        "_puffo_internal": "tool_result",
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "is_error": False,
    }
    if committed:
        native["provider_context_committed"] = True
        native["admission_binding"] = hashlib.sha256(
            f"{call_id}\x00{receipt}".encode("utf-8")
        ).hexdigest()
    return HarnessEvent.normalized(
        type="turn.tool_completed",
        driver="acp",
        session_ref=SessionRef(manager.native_session_id),
        turn_ref=driver.turn,
        native_session_id=manager.native_session_id,
        native_turn_id=manager.native_turn_id,
        data={"tool_call_ref": call_id, "label": tool_name, "outcome": "succeeded"},
        native_payload=native,
    )


def _recorder(sink):
    async def _cb(event):
        sink.append(event)
    return _cb


async def _fresh():
    driver = _ControllableDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"), driver_name="acp")
    await manager.open()
    await manager.start_turn(TurnInput("notice"))
    return driver, manager


@pytest.mark.asyncio
async def test_control_receipt_present_admits():
    """Positive control: with the receipt, the continuation IS released."""
    driver, manager = await _fresh()
    admitted = []
    manager.register_continuation(
        _recorder(admitted), "cycle", tool_names=("read_inbox",),
        tool_arguments={"limit": 1}, correlation_receipt="receipt-x",
    )
    await manager._admit_matching_tool_result(
        _acp_fact(manager, driver, call_id="c1", receipt="receipt-x", committed=True)
    )
    assert len(admitted) == 1
    assert not manager._continuation_admissions


@pytest.mark.asyncio
async def test_receipt_lost_on_acp_is_never_admitted():
    """The measurement: receipt lost -> fallback CANNOT rescue it on ACP."""
    driver, manager = await _fresh()
    admitted = []
    manager.register_continuation(
        _recorder(admitted), "cycle", tool_names=("read_inbox",),
        tool_arguments={"limit": 1}, correlation_receipt="receipt-x",
    )
    await manager._admit_matching_tool_result(
        _acp_fact(manager, driver, call_id="c1", receipt="receipt-x", committed=False)
    )
    assert admitted == [], "fallback unexpectedly released the admission"
    assert len(manager._continuation_admissions) == 1, "admission should still hang"


@pytest.mark.asyncio
async def test_lost_receipt_is_audible_when_the_turn_ends(caplog):
    """The Puffo-side signal: a continuation dropped un-released is logged."""
    driver, manager = await _fresh()
    admitted = []
    manager.register_continuation(
        _recorder(admitted), "cycle", tool_names=("read_inbox",),
        tool_arguments={"limit": 1}, correlation_receipt="receipt-x",
    )
    await manager._admit_matching_tool_result(
        _acp_fact(manager, driver, call_id="c1", receipt="receipt-x", committed=False)
    )
    assert admitted == []
    with caplog.at_level("WARNING"):
        manager._discard_pending_admissions("turn_completed")
    assert "continuation admissions discarded without a tool result" in caplog.text
    assert "count=1" in caplog.text


@pytest.mark.asyncio
async def test_a_released_continuation_leaves_no_warning(caplog):
    """Negative control: the signal must not fire when nothing was lost."""
    driver, manager = await _fresh()
    admitted = []
    manager.register_continuation(
        _recorder(admitted), "cycle", tool_names=("read_inbox",),
        tool_arguments={"limit": 1}, correlation_receipt="receipt-x",
    )
    await manager._admit_matching_tool_result(
        _acp_fact(manager, driver, call_id="c1", receipt="receipt-x", committed=True)
    )
    assert len(admitted) == 1
    with caplog.at_level("WARNING"):
        manager._discard_pending_admissions("turn_completed")
    assert "continuation admissions discarded" not in caplog.text


@pytest.mark.asyncio
async def test_deliberate_cancellation_stays_silent(caplog):
    """A signal that also fires on the intended case stops being read."""
    from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManagerAdapter

    driver, manager = await _fresh()
    admitted = []
    manager.register_continuation(
        _recorder(admitted), "cycle", tool_names=("read_inbox",),
        tool_arguments={"limit": 1}, correlation_receipt="receipt-x",
    )
    adapter = RuntimeManagerAdapter(manager)
    with caplog.at_level("WARNING"):
        adapter.register_continuation_callback(None)
    assert not manager._continuation_admissions
    assert "continuation admissions discarded" not in caplog.text
