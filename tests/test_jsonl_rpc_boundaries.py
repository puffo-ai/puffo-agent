"""Transport boundaries the JSONL Drivers must hold, checked by running them.

Four properties had no direct test, so a refactor of the RPC mechanics could
only be reviewed by reading the diff and agreeing with it:

* the driver owns its frame shape (Codex nests ``params``, Pi flattens them);
* a failing write leaves no registered request behind;
* a cancelled request leaves no registered request behind;
* only a *response* timeout becomes ``provider_unavailable`` -- a local write
  failure is not a verdict about the provider.

Plus two regression surfaces: the timeout message still names which provider
went silent, and blank-record policy stays per driver -- Pi's framing contract
has an empty-record clause and Codex's does not.

These are written against the Driver's public request path, not against the
shared helper, so they hold on either side of a transport refactor and say
whether behaviour moved.
"""
from __future__ import annotations

import asyncio
import warnings

import pytest

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver
from puffo_agent.agent.harness.drivers.pi import PiDriver


class _RecordingStdin:
    def __init__(self, fail: Exception | None = None) -> None:
        self.frames: list[bytes] = []
        self.fail = fail

    def write(self, data: bytes) -> None:
        if self.fail is not None:
            raise self.fail
        self.frames.append(data)

    async def drain(self) -> None:
        return None


class _TimeoutOnDrainStdin(_RecordingStdin):
    async def drain(self) -> None:
        raise TimeoutError("write stalled")


class _Proc:
    def __init__(self, stdin) -> None:
        self.stdin = stdin
        self.stdout = asyncio.StreamReader()
        self.stderr = None
        self.returncode = None


def _drivers():
    return [
        ("codex", CodexAppServerDriver(request_timeout_seconds=0.05)),
        ("pi", PiDriver(request_timeout_seconds=0.05)),
    ]


# -- A: frame construction stays with the driver ---------------------------

@pytest.mark.asyncio
async def test_each_driver_keeps_its_own_frame_shape():
    import json

    codex = CodexAppServerDriver(request_timeout_seconds=0.02)
    codex._proc = _Proc(_RecordingStdin())
    with pytest.raises(AgentAPIError):
        await codex._request("newThread", {"cwd": "/ws"})

    pi = PiDriver(request_timeout_seconds=0.02)
    pi._proc = _Proc(_RecordingStdin())
    with pytest.raises(AgentAPIError):
        await pi._request("get_session_stats", {"detail": "full"})

    codex_frame = json.loads(codex._proc.stdin.frames[0])
    pi_frame = json.loads(pi._proc.stdin.frames[0])

    # Codex nests params under "params"; Pi flattens them into the frame.
    assert codex_frame == {
        "id": 1, "method": "newThread", "params": {"cwd": "/ws"}
    }
    assert pi_frame == {
        "id": "puffo-1", "type": "get_session_stats", "detail": "full"
    }


# -- B: a failing write must not leak the pending entry --------------------

@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_write_failure_leaves_no_pending_entry(name, driver):
    driver._proc = _Proc(_RecordingStdin(fail=BrokenPipeError("child gone")))

    with pytest.raises(BrokenPipeError):
        await driver._request("anything", {})

    assert driver._pending == {}, f"{name} leaked a pending entry"


# -- D: only a response timeout is translated ------------------------------

@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_write_failure_is_not_reported_as_provider_unavailable(name, driver):
    driver._proc = _Proc(_RecordingStdin(fail=BrokenPipeError("child gone")))

    with pytest.raises(BaseException) as excinfo:
        await driver._request("anything", {})

    assert not isinstance(excinfo.value, AgentAPIError), (
        f"{name} converted a local write failure into a provider verdict"
    )


@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_write_timeout_is_not_reported_as_response_timeout(name, driver):
    driver._proc = _Proc(_TimeoutOnDrainStdin())

    with pytest.raises(TimeoutError, match="write stalled") as excinfo:
        await driver._request("anything", {})

    assert not isinstance(excinfo.value, AgentAPIError), (
        f"{name} converted a local write timeout into a provider verdict"
    )
    assert driver._pending == {}, f"{name} leaked a pending entry"


@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_response_timeout_is_a_bounded_retryable_provider_failure(name, driver):
    driver._proc = _Proc(_RecordingStdin())

    with pytest.raises(AgentAPIError) as excinfo:
        await driver._request("anything", {})

    assert excinfo.value.error_code == "provider_unavailable"
    assert excinfo.value.is_auth is False
    assert driver._pending == {}, f"{name} leaked a pending entry on timeout"


# -- E: the message still names which provider went silent -----------------

@pytest.mark.parametrize("name,driver,label", [
    ("codex", CodexAppServerDriver(request_timeout_seconds=0.02), "Codex"),
    ("pi", PiDriver(request_timeout_seconds=0.02), "Pi"),
])
@pytest.mark.asyncio
async def test_timeout_message_names_the_provider(name, driver, label):
    driver._proc = _Proc(_RecordingStdin())

    with pytest.raises(AgentAPIError) as excinfo:
        await driver._request("some_method", {})

    assert label in str(excinfo.value)
    assert "some_method" in str(excinfo.value)


# -- C: cancellation must not leak the pending entry -----------------------

@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_cancellation_leaves_no_pending_entry(name, driver):
    driver.request_timeout_seconds = 30.0
    driver._proc = _Proc(_RecordingStdin())

    task = asyncio.create_task(driver._request("anything", {}))
    for _ in range(50):
        await asyncio.sleep(0)
        if driver._pending:
            break
    assert driver._pending, f"{name}: request never registered"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert driver._pending == {}, f"{name} leaked a pending entry on cancel"


# -- the coroutine handed to the helper is always awaited ------------------

@pytest.mark.parametrize("name,driver", _drivers())
@pytest.mark.asyncio
async def test_no_unawaited_send_coroutine_on_the_failure_paths(name, driver):
    driver._proc = _Proc(_RecordingStdin(fail=BrokenPipeError("child gone")))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BrokenPipeError):
            await driver._request("anything", {})

    never_awaited = [w for w in caught if "never awaited" in str(w.message)]
    assert not never_awaited, f"{name}: {[str(w.message) for w in never_awaited]}"


# -- F: blank-record policy stays per driver -------------------------------

@pytest.mark.asyncio
async def test_blank_record_policy_did_not_become_shared():
    """Pi skips a blank record; Codex has no such clause and must not gain one."""
    from puffo_agent.agent.harness.driver import HarnessEventType

    pi = PiDriver()
    pi._proc = _Proc(_RecordingStdin())
    pi._proc.stdout.feed_data(b"\n")
    pi._proc.stdout.feed_eof()
    await pi._read_loop()
    pi_codes = []
    while not pi._events.empty():
        event = pi._events.get_nowait()
        if event is not None and event.type is HarnessEventType.RUNTIME_WARNING:
            pi_codes.append(event.data.get("code"))
    assert pi_codes == [], "Pi should treat a blank record as framing"

    codex = CodexAppServerDriver()
    codex._proc = _Proc(_RecordingStdin())
    codex._proc.stdout.feed_data(b"\n")
    codex._proc.stdout.feed_eof()
    await codex._read_loop()
    codex_codes = []
    while not codex._events.empty():
        event = codex._events.get_nowait()
        if event is not None and event.type is HarnessEventType.RUNTIME_WARNING:
            codex_codes.append(event.data.get("code"))
    assert codex_codes == ["protocol_parse"], (
        "Codex's framing contract has no blank-record clause; the shared "
        f"layer must not have given it one (got {codex_codes})"
    )


# -- B, properly: the response can arrive before the write call returns ----

class _AnsweringStdin:
    """A child that answers on the same tick the write drains.

    This is what makes register-before-write load-bearing. If registration
    happened after the write returned, the reader would look up an id that is
    not in `_pending` yet, drop the response, and the caller would time out on
    a request that was in fact answered.
    """

    def __init__(self, stdout: asyncio.StreamReader, reply: bytes) -> None:
        self.stdout = stdout
        self.reply = reply
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        self.stdout.feed_data(self.reply)
        for _ in range(10):  # let the reader run before the write returns
            await asyncio.sleep(0)


@pytest.mark.parametrize("name,reply", [
    ("codex", b'{"id":1,"result":{"ok":true}}\n'),
    ("pi", b'{"type":"response","command":"anything","id":"puffo-1",'
           b'"success":true,"data":{"ok":true}}\n'),
])
@pytest.mark.asyncio
async def test_a_response_racing_the_write_is_not_dropped(name, reply):
    driver = (
        CodexAppServerDriver(request_timeout_seconds=0.2)
        if name == "codex"
        else PiDriver(request_timeout_seconds=0.2)
    )
    stdout = asyncio.StreamReader()
    driver._proc = _Proc(_AnsweringStdin(stdout, reply))
    driver._proc.stdout = stdout
    reader = asyncio.create_task(driver._read_loop())

    result = await driver._request("anything", {})

    assert result is not None, (
        f"{name} dropped a response that arrived before the write returned; "
        "the request must be registered before it is sent"
    )
    assert driver._pending == {}
    stdout.feed_eof()
    await asyncio.wait_for(reader, timeout=1)
