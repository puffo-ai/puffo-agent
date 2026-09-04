"""OpenCode native compact: transient serve + summarize, without the manager race.

``RuntimeManager.begin_compaction()`` awaits ``driver.compact()`` BEFORE it
creates the future it later resolves on COMPACTION_COMPLETED. A compact()
that finishes the work synchronously would emit into a void and hang the
caller for its full timeout. The contract pinned here: compact() only
validates and spawns a task; no compaction event may be enqueued before it
returns.

Failure surface: provider errors emit COMPACTION_FAILED — never a fake
COMPACTED, never RUNTIME_EXITED (which would retire a session that is
still perfectly usable) — and the manager fails its outstanding future
with the diagnostic instead of letting callers wait out their timeout.
"""

import asyncio
import json

import pytest

from puffo_agent.agent.harness.driver import (
    CompactRequest,
    HarnessEventType,
    RuntimeSpec,
    TurnInput,
)
from puffo_agent.agent.harness.drivers.opencode import (
    OpenCodeDriver,
    _require_summarize_ack,
    _server_url_from_streams,
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


async def _idle_driver_with_session(procs):
    """Open a driver and complete one turn so a native session exists."""

    def factory(command, spec):
        return procs.pop(0)

    driver = OpenCodeDriver(factory)
    await driver.open(RuntimeSpec("/workspace", model="opencode/hy3-free"))
    stream = driver.events()
    proc = procs[0] if procs else None
    started = asyncio.create_task(driver.start_turn(TurnInput("hi")))
    proc.feed({"type": "step_start", "sessionID": "ses_c",
               "part": {"messageID": "msg_1"}})
    await asyncio.wait_for(started, timeout=1)
    proc.exit(0)
    proc.eof()
    async for event in stream:
        if event.type is HarnessEventType.TURN_COMPLETED:
            break
    return driver, stream


async def _drain_compaction(stream):
    seen = []
    async for event in stream:
        if event.type in (
            HarnessEventType.COMPACTION_STARTED,
            HarnessEventType.COMPACTION_COMPLETED,
            HarnessEventType.COMPACTION_FAILED,
        ):
            seen.append(event)
            if event.type is not HarnessEventType.COMPACTION_STARTED:
                return seen
    raise AssertionError("stream ended before a terminal compaction event")


@pytest.mark.asyncio
async def test_compact_returns_before_any_compaction_event_is_enqueued(monkeypatch):
    procs = [_TurnProcess(), _TurnProcess()]
    driver, stream = await _idle_driver_with_session([procs[0], procs[1]])

    async def instant_success(spec, native_session_id):
        return None

    monkeypatch.setattr(driver, "_summarize_via_serve", instant_success)
    receipt = await driver.compact(CompactRequest())
    assert receipt.accepted and receipt.operation_ref
    # The manager creates its completion future only after compact()
    # returns; nothing may have been emitted yet.
    assert driver._events.empty()

    seen = await _drain_compaction(stream)
    assert [e.type for e in seen] == [
        HarnessEventType.COMPACTION_STARTED,
        HarnessEventType.COMPACTION_COMPLETED,
    ]
    assert seen[-1].data["operation_ref"] == receipt.operation_ref
    await driver.close()


@pytest.mark.asyncio
async def test_provider_failure_emits_compaction_failed_and_keeps_session(monkeypatch):
    turn2 = _TurnProcess()
    driver, stream = await _idle_driver_with_session([_TurnProcess(), turn2])

    async def provider_error(spec, native_session_id):
        raise RuntimeError("summarize returned HTTP 500: provider melted")

    monkeypatch.setattr(driver, "_summarize_via_serve", provider_error)
    receipt = await driver.compact(CompactRequest())
    seen = await _drain_compaction(stream)
    assert seen[-1].type is HarnessEventType.COMPACTION_FAILED
    assert "500" in seen[-1].data["diagnostic"]
    assert seen[-1].data["operation_ref"] == receipt.operation_ref

    # The session survives a failed compaction: the next turn still runs.
    started = asyncio.create_task(driver.start_turn(TurnInput("again")))
    turn2.feed({"type": "step_start", "sessionID": "ses_c",
                "part": {"messageID": "msg_2"}})
    accepted = await asyncio.wait_for(started, timeout=1)
    assert accepted.accepted
    await driver.close()


@pytest.mark.asyncio
async def test_compact_validates_idle_session_and_model():
    driver = OpenCodeDriver(lambda command, spec: _TurnProcess())
    with pytest.raises(RuntimeError, match="not open"):
        await driver.compact(CompactRequest())

    await driver.open(RuntimeSpec("/workspace", model="opencode/hy3-free"))
    with pytest.raises(RuntimeError, match="no native session"):
        await driver.compact(CompactRequest())
    await driver.close()

    no_model = OpenCodeDriver(lambda command, spec: _TurnProcess())
    await no_model.open(RuntimeSpec("/workspace"))
    no_model._native_session_id = "ses_x"
    with pytest.raises(RuntimeError, match="provider"):
        await no_model.compact(CompactRequest())
    await no_model.close()


@pytest.mark.asyncio
async def test_concurrent_compact_coalesces_onto_the_same_operation(monkeypatch):
    driver, stream = await _idle_driver_with_session([_TurnProcess()])
    release = asyncio.Event()

    async def blocked(spec, native_session_id):
        await release.wait()

    monkeypatch.setattr(driver, "_summarize_via_serve", blocked)
    first = await driver.compact(CompactRequest())
    second = await driver.compact(CompactRequest())
    assert second.operation_ref == first.operation_ref
    release.set()
    await _drain_compaction(stream)
    await driver.close()


@pytest.mark.asyncio
async def test_server_url_parsed_from_either_stream():
    class _Proc:
        def __init__(self, out_lines, err_lines):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            for line in out_lines:
                self.stdout.feed_data(line)
            self.stdout.feed_eof()
            for line in err_lines:
                self.stderr.feed_data(line)
            self.stderr.feed_eof()

    url = await _server_url_from_streams(_Proc(
        [b"Warning: unsecured\n",
         b"opencode server listening on http://127.0.0.1:63757\n"],
        [],
    ))
    assert url == "http://127.0.0.1:63757"

    url = await _server_url_from_streams(_Proc(
        [b"plugin noise: listening on http://evil.example:9999\n"],
        [b"opencode server listening on http://127.0.0.1:4096\n"],
    ))
    assert url == "http://127.0.0.1:4096"

    with pytest.raises(RuntimeError, match="exited before announcing"):
        await _server_url_from_streams(_Proc(
            [b"plugin noise: listening on http://evil.example:9999\n"],
            [b"no trusted url here\n"],
        ))


@pytest.mark.asyncio
async def test_server_url_waits_when_one_stream_closes_first():
    class _Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

    proc = _Proc()
    reading = asyncio.create_task(_server_url_from_streams(proc))
    proc.stdout.feed_eof()
    await asyncio.sleep(0)
    assert not reading.done()

    proc.stderr.feed_data(
        b"opencode server listening on http://127.0.0.1:4096\n"
    )
    assert await asyncio.wait_for(reading, timeout=1) == "http://127.0.0.1:4096"


def test_summarize_requires_documented_true_acknowledgement():
    _require_summarize_ack(200, "true")
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _require_summarize_ack(503, '"not available"')
    with pytest.raises(RuntimeError, match="non-JSON"):
        _require_summarize_ack(200, "okay")
    with pytest.raises(RuntimeError, match="unexpected acknowledgement"):
        _require_summarize_ack(200, "false")
