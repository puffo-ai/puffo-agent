import asyncio
import json
import os
import sys
import time

import psutil
import pytest

from puffo_agent.agent.harness import build_driver
from puffo_agent.agent.harness.support.cleanup_errors import cleanup_errors
from puffo_agent.agent.harness.driver import (
    BusyDelivery,
    HarnessEventType,
    RuntimeLifecycle,
    RuntimeSpec,
    TurnInput,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.drivers.opencode import (
    OPENCODE_CAPABILITIES,
    OpenCodeDriver,
)
from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManager
from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManagerAdapter
from puffo_agent.agent.harness.support.subprocess_io import ProcessTreeShutdownError


class _TurnProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.terminated = 0
        self._exit = asyncio.get_running_loop().create_future()

    def feed(self, frame: dict) -> None:
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def feed_raw_json(self, frame: dict) -> None:
        self.stdout.feed_data(
            json.dumps(frame, ensure_ascii=False).encode() + b"\n"
        )

    def eof(self) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def exit(self, returncode: int = 0) -> None:
        self.returncode = returncode
        if not self._exit.done():
            self._exit.set_result(returncode)

    def terminate(self) -> None:
        self.terminated += 1
        self.exit(-15)
        self.eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return await self._exit


class _HungTurnProcess(_TurnProcess):
    def __init__(self) -> None:
        super().__init__()
        self.killed = 0

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1
        self.exit(-9)
        self.eof()


class _UncloseableTurnProcess(_HungTurnProcess):
    def kill(self) -> None:
        self.killed += 1
        self.exit(-9)


async def _next_matching(stream, type_: HarnessEventType):
    async for event in stream:
        if event.type is type_:
            return event
    raise AssertionError(f"event stream ended before {type_.value}")


async def _collect_through(stream, type_: HarnessEventType):
    events = []
    async for event in stream:
        events.append(event)
        if event.type is type_:
            return events
    raise AssertionError(f"event stream ended before {type_.value}")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_signal", ["exit", "eof"])
async def test_turn_terminal_waits_for_both_process_exit_and_stream_eof(
    first_signal,
):
    proc = _TurnProcess()
    commands = []

    def factory(command, _spec):
        commands.append(command)
        return proc

    driver = OpenCodeDriver(factory)
    opened = await driver.open(RuntimeSpec("/workspace"))
    assert opened.native_session_id == ""
    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_1",
        "part": {"messageID": "msg_1"},
    })
    receipt = await asyncio.wait_for(started, timeout=1)
    assert receipt.native_turn_id == "msg_1"
    terminal = asyncio.create_task(
        _next_matching(stream, HarnessEventType.TURN_COMPLETED)
    )

    getattr(proc, first_signal)()
    await asyncio.sleep(0)
    assert not terminal.done()
    getattr(proc, "eof" if first_signal == "exit" else "exit")()

    event = await asyncio.wait_for(terminal, timeout=1)
    assert event.data == {"outcome": "succeeded"}
    assert commands[0][-1] == "hello"
    await driver.close()


@pytest.mark.asyncio
async def test_second_turn_resumes_native_session_and_emits_one_session_boundary():
    processes = [_TurnProcess(), _TurnProcess()]
    commands = []

    def factory(command, _spec):
        commands.append(command)
        return processes[len(commands) - 1]

    driver = OpenCodeDriver(factory)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    events = []

    for index, proc in enumerate(processes, start=1):
        started = asyncio.create_task(
            driver.start_turn(TurnInput(f"turn {index}"))
        )
        proc.feed({
            "type": "step_start",
            "sessionID": "ses_1",
            "part": {"messageID": f"msg_{index}"},
        })
        await asyncio.wait_for(started, timeout=1)
        proc.eof()
        proc.exit()
        events.extend(
            await asyncio.wait_for(
                _collect_through(stream, HarnessEventType.TURN_COMPLETED),
                timeout=1,
            )
        )

    assert "--session" not in commands[0]
    session_index = commands[1].index("--session")
    assert commands[1][session_index + 1] == "ses_1"
    await driver.close()
    assert sum(
        event.type
        in {HarnessEventType.SESSION_OPENED, HarnessEventType.SESSION_RESUMED}
        for event in events
    ) == 1


@pytest.mark.asyncio
async def test_cancel_terminates_child_and_emits_one_abandon_terminal():
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_1",
        "part": {"messageID": "msg_1"},
    })
    receipt = await started

    cancel = await driver.cancel_turn(receipt.turn_ref)
    assert cancel.accepted is True
    terminal = await asyncio.wait_for(
        _next_matching(stream, HarnessEventType.TURN_ABANDONED), timeout=1
    )
    assert terminal.data["error_code"] == "cancelled"
    assert proc.terminated == 1
    await driver.close()


@pytest.mark.asyncio
async def test_start_rejects_process_that_never_emits_valid_acceptance_frame():
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.stdout.feed_data(b"not-json\n")
    proc.eof()
    proc.exit(2)

    with pytest.raises(RuntimeError, match="before accepting"):
        await asyncio.wait_for(started, timeout=1)
    await driver.close()


@pytest.mark.asyncio
async def test_process_factory_type_error_is_not_retried():
    calls = 0

    def factory(_command, _spec):
        nonlocal calls
        calls += 1
        raise TypeError("factory failed after side effect")

    driver = OpenCodeDriver(factory)
    await driver.open(RuntimeSpec("/workspace"))

    with pytest.raises(TypeError, match="factory failed after side effect"):
        await driver.start_turn(TurnInput("hello"))
    assert calls == 1
    await driver.close()


@pytest.mark.asyncio
async def test_jsonl_reader_does_not_split_valid_unicode_line_separators():
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    text = "one\u2028two\u2029three\u0085four"
    proc.feed_raw_json({
        "type": "text",
        "sessionID": "ses_1",
        "part": {"id": "part_1", "messageID": "msg_1", "text": text},
    })
    await asyncio.wait_for(started, timeout=1)
    proc.eof()
    proc.exit()

    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=1
    )
    [delta] = [
        event for event in events
        if event.type is HarnessEventType.ASSISTANT_DELTA
    ]
    assert delta.data["delta"] == text
    await driver.close()


def test_capabilities_and_factory_declare_per_turn_reject_contract():
    assert isinstance(build_driver("opencode"), OpenCodeDriver)
    assert OPENCODE_CAPABILITIES.lifecycle is RuntimeLifecycle.PER_TURN_CHILD
    assert OPENCODE_CAPABILITIES.busy_delivery is BusyDelivery.REJECT
    assert OPENCODE_CAPABILITIES.steer == "none"
    assert isinstance(
        asyncio.run(OpenCodeDriver().steer_turn(None, TurnInput("x"))),
        UnsupportedCapability,
    )


@pytest.mark.asyncio
async def test_manager_adopts_session_learned_by_per_turn_child():
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    manager = RuntimeManager(
        driver,
        RuntimeSpec("/workspace"),
        driver_name="opencode",
    )
    await manager.open()
    started = asyncio.create_task(manager.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_discovered",
        "part": {"messageID": "msg_1"},
    })
    receipt = await asyncio.wait_for(started, timeout=1)
    assert manager.native_session_id == "ses_discovered"
    assert manager.opened is not None
    assert manager.opened.native_session_id == "ses_discovered"
    assert RuntimeManagerAdapter(manager).get_provider_session_id() == (
        "ses_discovered"
    )
    proc.eof()
    proc.exit()
    terminal = await asyncio.wait_for(
        manager.wait_terminal(receipt.turn_ref), timeout=1
    )
    assert terminal.type is HarnessEventType.TURN_COMPLETED
    await manager.close()


@pytest.mark.asyncio
async def test_error_frame_detail_reaches_the_failed_turn():
    """Provider errors stay distinguishable without leaking raw payloads."""
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda _command, _spec: proc)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_1",
        "part": {"messageID": "msg_1"},
    })
    await asyncio.wait_for(started, timeout=1)
    proc.feed({
        "type": "error",
        "sessionID": "ses_1",
        "error": {
            "name": "UnknownError",
            "data": {
                "message": (
                    "Unexpected server error. "
                    "Authorization: Basic dXNlcjpwYXNz, ref err_3bf8"
                )
            },
        },
    })
    proc.exit(1)
    proc.eof()

    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=1
    )

    failed_frame = next(
        event
        for event in events
        if event.type is HarnessEventType.RUNTIME_FAILED
    )
    assert "Unexpected server error" in failed_frame.data.get("diagnostic", "")
    assert "dXNlcjpwYXNz" not in failed_frame.data.get("diagnostic", "")
    terminal = events[-1]
    assert terminal.data["outcome"] == "failed"
    assert "Unexpected server error" in terminal.data.get("diagnostic", "")
    assert "dXNlcjpwYXNz" not in terminal.data.get("diagnostic", "")
    await driver.close()


@pytest.mark.asyncio
async def test_pre_acceptance_exit_carries_redacted_stderr_tail():
    """Pre-acceptance stderr reaches the caller, but credentials do not."""
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda _command, _spec: proc)
    await driver.open(RuntimeSpec("/workspace"))

    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await asyncio.sleep(0)
    proc.stderr.feed_data(
        b"Session not found; rejected api_key=sk_live_abcdef1234567890; "
        b"Authorization: Basic dXNlcjpwYXNz, denied\n"
    )
    proc.exit(1)
    proc.eof()

    with pytest.raises(RuntimeError) as exc_info:
        await asyncio.wait_for(started, timeout=1)

    diagnostic = str(exc_info.value)
    assert "Session not found" in diagnostic
    assert "sk_live_abcdef1234567890" not in diagnostic
    assert "dXNlcjpwYXNz" not in diagnostic
    assert "[REDACTED]" in diagnostic
    await driver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_exited_child_with_open_stderr_cannot_block_turn_boundary(
    monkeypatch, accepted
):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.opencode."
        "_STDERR_TAIL_GRACE_SECONDS",
        0.01,
    )
    proc = _TurnProcess()
    driver = OpenCodeDriver(lambda _command, _spec: proc)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))

    if accepted:
        proc.feed({
            "type": "step_start",
            "sessionID": "ses_1",
            "part": {"messageID": "msg_1"},
        })
        await asyncio.wait_for(started, timeout=1)

    # Model a descendant that inherited stderr after the direct child exits.
    proc.stdout.feed_eof()
    proc.exit(1)

    if accepted:
        terminal = await asyncio.wait_for(
            _next_matching(stream, HarnessEventType.TURN_COMPLETED), timeout=1
        )
        assert terminal.data["outcome"] == "failed"
    else:
        with pytest.raises(RuntimeError, match="before accepting"):
            await asyncio.wait_for(started, timeout=1)

    assert driver._stderr_reader is None
    await driver.close()


@pytest.mark.asyncio
async def test_close_kills_then_cancels_a_child_with_inherited_stdout(
    monkeypatch,
):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.opencode._SHUTDOWN_GRACE_SECONDS",
        0.01,
    )
    proc = _HungTurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_hung",
        "part": {"messageID": "msg_hung"},
    })
    await asyncio.wait_for(started, timeout=1)

    await asyncio.wait_for(driver.close(), timeout=1)

    assert proc.terminated == 1
    assert proc.killed == 1
    assert driver._turn_task is None


def test_default_spawn_isolates_the_per_turn_process_group(monkeypatch):
    from puffo_agent.agent.harness.support import subprocess_io

    monkeypatch.setattr(subprocess_io.os, "name", "posix")
    kwargs = subprocess_io.process_group_spawn_kwargs()
    assert kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_force_signal_targets_group_after_direct_child_has_exited(
    monkeypatch,
):
    from puffo_agent.agent.harness.support import subprocess_io

    class ExitedParent:
        pid = 4321
        returncode = -15

    signals = []
    monkeypatch.setattr(subprocess_io.os, "name", "posix")
    monkeypatch.setattr(
        subprocess_io.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    await subprocess_io.signal_process_tree(
        ExitedParent(), force=True, timeout=0.1
    )

    assert signals == [(4321, subprocess_io.signal.SIGKILL)]


@pytest.mark.asyncio
async def test_missing_process_group_falls_back_to_direct_child(monkeypatch):
    from puffo_agent.agent.harness.support import subprocess_io

    class UngroupedProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.terminated = 0

        def terminate(self):
            self.terminated += 1

    proc = UngroupedProcess()
    monkeypatch.setattr(subprocess_io.os, "name", "posix")
    monkeypatch.setattr(
        subprocess_io.os,
        "killpg",
        lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
    )

    await subprocess_io.signal_process_tree(proc, force=False, timeout=0.1)

    assert proc.terminated == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_is_not_process_exit_evidence():
    from puffo_agent.agent.harness.support import subprocess_io

    proc = type("RunningProcess", (), {"returncode": None})()
    waiter = asyncio.create_task(asyncio.sleep(60))
    waiter.cancel()

    assert not await subprocess_io._waiter_settled(proc, waiter, 0.1)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
async def test_close_reaps_descendant_that_holds_inherited_stdout(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.opencode._SHUTDOWN_GRACE_SECONDS",
        0.2,
    )
    child_script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    pid_path = tmp_path / "descendant.pid"
    parent_script = (
        "import json, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        f"{child_script!r}], stdout=sys.stdout, stderr=sys.stderr); "
        f"open({str(pid_path)!r}, 'w').write(str(child.pid)); "
        "print(json.dumps({'type': 'step_start', 'sessionID': 'ses_tree', "
        "'part': {'messageID': 'msg_tree'}}), flush=True); "
        "time.sleep(60)"
    )

    spawned = {}

    async def factory(_command, _spec):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned["proc"] = proc
        return proc

    driver = OpenCodeDriver(factory)
    await driver.open(RuntimeSpec("/workspace"))
    await asyncio.wait_for(
        driver.start_turn(TurnInput("hello")), timeout=1
    )

    await asyncio.wait_for(driver.close(), timeout=2)

    descendant_pid = int(pid_path.read_text())
    _assert_process_exited(descendant_pid)
    proc = spawned["proc"]
    assert proc._transport.is_closing()
    assert proc.stdout._transport.is_closing()
    assert proc.stderr._transport.is_closing()
    assert driver._turn_task is None


@pytest.mark.asyncio
async def test_close_reports_transport_abandonment(monkeypatch):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.opencode._SHUTDOWN_GRACE_SECONDS",
        0.01,
    )
    proc = _UncloseableTurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    proc.feed({
        "type": "step_start",
        "sessionID": "ses_timeout",
        "part": {"messageID": "msg_timeout"},
    })
    await started

    with pytest.raises(ProcessTreeShutdownError):
        await driver.close()


@pytest.mark.asyncio
async def test_cancelled_start_preserves_cancellation_and_shutdown_error(
    monkeypatch,
):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.opencode._SHUTDOWN_GRACE_SECONDS",
        0.01,
    )
    proc = _UncloseableTurnProcess()
    driver = OpenCodeDriver(lambda *_args: proc)
    await driver.open(RuntimeSpec("/workspace"))
    task = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    attached = cleanup_errors(exc_info.value)
    assert len(attached) == 1
    assert isinstance(attached[0], ProcessTreeShutdownError)
    assert proc.returncode == -9
    assert driver._proc is None
    assert driver._turn_task is None
    assert not driver._active.value


@pytest.mark.asyncio
async def test_failed_start_and_shutdown_failure_are_grouped(monkeypatch):
    driver = OpenCodeDriver(lambda *_args: (_ for _ in ()).throw(
        ValueError("primary start failure")
    ))
    await driver.open(RuntimeSpec("/workspace"))

    async def fail_cleanup(_generation):
        raise ProcessTreeShutdownError("cleanup failure")

    monkeypatch.setattr(driver, "_abort_failed_start", fail_cleanup)

    with pytest.raises(ExceptionGroup) as exc_info:
        await driver.start_turn(TurnInput("hello"))

    assert [str(exc) for exc in exc_info.value.exceptions] == [
        "primary start failure",
        "cleanup failure",
    ]


def _assert_process_exited(pid: int) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.01)
    raise AssertionError(f"descendant process {pid} is still alive")
