"""OpenCode context-status push from step_finish totals.

Measured on opencode 1.18.16: ``step_finish.part.tokens.total`` is the
provider-reported context occupancy of that step's call (input +
cache.read + output add up exactly), not a session-cumulative counter.
The driver absorbs each push into its cached ``ContextStatus`` — nothing
estimated — and augments the CONTEXT_UPDATED event with
``used_tokens``/``context_window`` for parity with the codex driver's
shape. The model window comes from ``opencode models <provider>
--verbose`` output, matched on the JSON object's ``id`` field.
"""

import asyncio
import json

import pytest

from puffo_agent.agent.harness.driver import (
    ContextStatusCapability,
    HarnessEventType,
    RuntimeSpec,
    TurnInput,
)
from puffo_agent.agent.harness.drivers.opencode import (
    OPENCODE_CAPABILITIES,
    OpenCodeDriver,
    _window_from_models_output,
)


class _TurnProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self._exit = asyncio.get_running_loop().create_future()

    def feed(self, frame: dict) -> None:
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def eof(self) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def exit(self, returncode: int = 0) -> None:
        self.returncode = returncode
        if not self._exit.done():
            self._exit.set_result(returncode)

    def terminate(self) -> None:
        self.exit(-15)
        self.eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return await self._exit


def _step_finish(total: int | None) -> dict:
    tokens = {
        "input": 222,
        "output": 9,
        "reasoning": 0,
        "cache": {"read": 9600, "write": 0},
    }
    if total is not None:
        tokens["total"] = total
    return {
        "type": "step_finish",
        "sessionID": "ses_ctx",
        "part": {"messageID": "msg_1", "tokens": tokens},
    }


def test_capability_declares_push_not_none():
    assert OPENCODE_CAPABILITIES.context_status is ContextStatusCapability.PUSH


@pytest.mark.asyncio
async def test_window_lookup_finishes_before_first_turn_can_start(monkeypatch):
    """Open cannot expose a driver while its metadata process is active."""
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda command, spec: proc)
    lookup_started = asyncio.Event()
    lookup_release = asyncio.Event()

    async def fake_lookup(spec):
        lookup_started.set()
        await lookup_release.wait()

    monkeypatch.setattr(driver, "_resolve_context_window", fake_lookup)
    opening = asyncio.create_task(
        driver.open(RuntimeSpec("/workspace", model="opencode/hy3-free"))
    )
    await asyncio.wait_for(lookup_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not opening.done()
    lookup_release.set()
    await asyncio.wait_for(opening, timeout=1)
    await driver.close()


@pytest.mark.asyncio
async def test_step_finish_total_becomes_context_status_and_augments_event():
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda command, spec: proc)
    await driver.open(RuntimeSpec("/workspace"))

    before = await driver.context_status()
    assert before.stale and before.used_tokens is None

    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hi")))
    proc.feed({"type": "step_start", "sessionID": "ses_ctx",
               "part": {"messageID": "msg_1"}})
    await asyncio.wait_for(started, timeout=1)

    proc.feed(_step_finish(total=9831))
    event = None
    async for candidate in stream:
        if candidate.type is HarnessEventType.CONTEXT_UPDATED:
            event = candidate
            break
    assert event is not None
    assert event.data["used_tokens"] == 9831
    assert event.data["total_tokens"] == 9831

    status = await driver.context_status()
    assert status.used_tokens == 9831
    assert not status.stale
    assert status.measured_at

    # A step_finish without a usable total must not clobber the last
    # good measurement (an aborted step reports zeros).
    proc.feed(_step_finish(total=None))
    async for candidate in stream:
        if candidate.type is HarnessEventType.CONTEXT_UPDATED:
            break
    still = await driver.context_status()
    assert still.used_tokens == 9831
    assert not still.stale

    await driver.close()
    closed = await driver.context_status()
    assert closed.stale


_MODELS_OUTPUT = """\
opencode/big-pickle
{
  "id": "big-pickle",
  "providerID": "opencode",
  "limit": {
    "context": 200000,
    "input": 160000,
    "output": 32000
  }
}
opencode/hy3-free
{
  "id": "hy3-free",
  "providerID": "opencode",
  "limit": {
    "context": 190000,
    "output": 32000
  }
}
"""


def test_window_parser_matches_on_object_id_not_line_pairing():
    assert _window_from_models_output(_MODELS_OUTPUT, "hy3-free") == 190000
    assert _window_from_models_output(_MODELS_OUTPUT, "big-pickle") == 200000
    assert _window_from_models_output(_MODELS_OUTPUT, "absent-model") is None
    assert _window_from_models_output("not json at all { broken", "x") is None
    assert _window_from_models_output("", "x") is None
