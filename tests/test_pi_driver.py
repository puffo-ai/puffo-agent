"""Pi RPC Driver behaviour, against the pinned 0.84.3 protocol surface.

The scripted child below speaks real bytes through an ``asyncio.StreamReader``
so the framing, correlation, and terminal-arbitration paths under test are the
ones the production reader runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from puffo_agent.agent.adapters.base import TurnContext
from puffo_agent.agent.harness.driver import (
    BusyDelivery,
    CompactRequest,
    ContextStatusCapability,
    HarnessEventType,
    PermissionDecision,
    PermissionRef,
    RuntimeLifecycle,
    RuntimeSpec,
    SessionRef,
    SteerCapability,
    TurnInput,
    TurnRef,
    UnsupportedCapability,
)
from puffo_agent.agent.harness import build_driver
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
)
from puffo_agent.agent.harness.drivers.pi_bridge import (
    BRIDGE_NONCE_ENV,
    BRIDGE_READY_FILE_ENV,
)
from puffo_agent.agent.harness.drivers.pi import (
    PI_AGENT_DIR_ENV,
    PI_CAPABILITIES,
    PiDriver,
    PiToolBridgeUnavailableError,
    verify_pi_tool_bridge,
)
from puffo_agent.agent.harness.drivers.pi_protocol import (
    EXTENSION_UI_DIALOG_METHODS,
    EXTENSION_UI_FIRE_AND_FORGET_METHODS,
    build_pi_launch_command,
    normalize_pi_event,
)

from .fixtures.pi_rpc_0_84_3 import (
    COMMANDS,
    DECLARED_CAPABILITIES,
    EVENTS,
    EXTENSION_UI_REQUESTS,
)

SESSION_FILE = "/sessions/abc123.jsonl"

# Set per test by the autouse fixture below; every spec points at a Pi config
# directory that contains an installed Puffo tool bridge.
_AGENT_DIR = ""
_READY_FILE = ""
_NONCE = "test-nonce"


@pytest.fixture(autouse=True)
def _installed_tool_bridge(tmp_path):
    global _AGENT_DIR, _READY_FILE
    extensions = tmp_path / "extensions"
    extensions.mkdir()
    (extensions / "puffo-tools.ts").write_text("// puffo tool bridge")
    _AGENT_DIR = str(tmp_path)
    _READY_FILE = str(tmp_path / "puffo-bridge-ready.json")
    yield
    _AGENT_DIR = ""
    _READY_FILE = ""


class FakeStdin:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.split(b"\n"):
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        return None


class FakePiProcess:
    """A ``pi --mode rpc`` child that answers commands and can push events."""

    def __init__(self, *, stats: dict | None = None, switch_cancelled=False):
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = None
        self.returncode = None
        self._stats = (
            stats
            if stats is not None
            else {
                "sessionFile": SESSION_FILE,
                "sessionId": "abc123",
                "contextUsage": {
                    "tokens": 60000,
                    "contextWindow": 200000,
                    "percent": 30,
                },
            }
        )
        self._switch_cancelled = switch_cancelled
        self.failed_commands: set[str] = set()

    # -- child -> client ---------------------------------------------------

    def push(self, frame: dict) -> None:
        self.stdout.feed_data(
            json.dumps(frame, ensure_ascii=False).encode() + b"\n"
        )

    def push_raw(self, payload: bytes) -> None:
        self.stdout.feed_data(payload)

    def eof(self) -> None:
        self.stdout.feed_eof()

    async def answer_next(self) -> dict:
        """Wait for the next client command and emit its documented response."""
        seen = len(self.stdin.frames)
        for _ in range(500):
            await asyncio.sleep(0)
            if len(self.stdin.frames) > seen:
                break
        else:  # pragma: no cover - only on a genuine hang
            raise AssertionError("client sent no command")
        frame = self.stdin.frames[-1]
        command = frame.get("type")
        if command == "extension_ui_response":
            return frame
        response = {
            "type": "response",
            "command": command,
            "success": command not in self.failed_commands,
            "id": frame.get("id"),
        }
        if command == "get_session_stats":
            response["data"] = self._stats
        elif command == "switch_session":
            response["data"] = {"cancelled": self._switch_cancelled}
        self.push(response)
        return frame

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self) -> None:  # pragma: no cover - close path uses terminate
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _spec(**overrides) -> RuntimeSpec:
    return RuntimeSpec(
        workspace_dir="/tmp/ws",
        executable="pi",
        environment={
            PI_AGENT_DIR_ENV: _AGENT_DIR,
            BRIDGE_READY_FILE_ENV: _READY_FILE,
            BRIDGE_NONCE_ENV: _NONCE,
        },
        **overrides,
    )


def _attesting_factory(proc: FakePiProcess, *, tools: int = 3):
    """Stand in for Pi loading the extension, which writes the attestation."""

    def factory(spec):
        ready = spec.environment.get(BRIDGE_READY_FILE_ENV)
        if ready and tools:
            Path(ready).write_text(
                json.dumps({"nonce": spec.environment[BRIDGE_NONCE_ENV],
                            "tools": tools})
            )
        return proc

    return factory


async def _open(proc: FakePiProcess, resume: SessionRef | None = None):
    driver = PiDriver(
        process_factory=_attesting_factory(proc), bridge_ready_timeout=1.0
    )
    task = asyncio.create_task(driver.open(_spec(), resume))
    if resume is not None:
        await proc.answer_next()
    await proc.answer_next()
    opened = await task
    # open() announces the session on the same queue the tests drain; consume
    # it here so each test asserts on the events it actually produced.
    await _drain_events(driver, 1)
    return driver, opened


async def _drain_events(driver: PiDriver, count: int, timeout: float = 2.0):
    events = []
    stream = driver.events()
    async with asyncio.timeout(timeout):
        async for event in stream:
            events.append(event)
            if len(events) >= count:
                break
    return events


# -- capability declaration -------------------------------------------------


def test_capabilities_match_the_pinned_protocol_surface():
    """The declared bits are the ones T0 derived from the shipped bundle.

    Pinning the driver to the fixture keeps a capability from being widened by
    hand without re-reading the protocol it claims to describe.
    """
    assert PI_CAPABILITIES.lifecycle == RuntimeLifecycle(
        DECLARED_CAPABILITIES["lifecycle"]
    )
    assert PI_CAPABILITIES.busy_delivery == BusyDelivery(
        DECLARED_CAPABILITIES["busy_delivery"]
    )
    assert PI_CAPABILITIES.steer == SteerCapability(
        DECLARED_CAPABILITIES["steer"]
    )
    assert PI_CAPABILITIES.context_status == ContextStatusCapability(
        DECLARED_CAPABILITIES["context_status"]
    )
    assert PI_CAPABILITIES.session_resume is DECLARED_CAPABILITIES[
        "session_resume"
    ]
    assert PI_CAPABILITIES.permission_bridge is DECLARED_CAPABILITIES[
        "permission_bridge"
    ]
    assert "steer" in COMMANDS
    assert "abort" in COMMANDS
    assert "get_session_stats" in COMMANDS
    assert "compact" in COMMANDS
    assert "switch_session" in COMMANDS


def test_launch_command_never_disables_session_persistence():
    """``--no-session`` would make the declared session_resume undeliverable."""
    assert "--no-session" not in build_pi_launch_command(_spec())
    assert build_pi_launch_command(_spec())[:3] == ("pi", "--mode", "rpc")


def test_launch_command_preserves_explicit_provider_and_model():
    command = build_pi_launch_command(
        _spec(model="gpt-5.5", launch_args=("--provider", "openai"))
    )
    assert command == (
        "pi", "--mode", "rpc", "--model", "gpt-5.5",
        "--provider", "openai",
    )


# -- exhaustive event normalization ----------------------------------------


@pytest.mark.parametrize("event_type", sorted(EVENTS))
def test_every_pinned_event_has_an_explicit_branch(event_type):
    """No pinned event may reach the unknown-event tail.

    The tail exists to report a protocol change we have not read. If a shipped
    event lands there, the driver is silently mis-describing the harness.
    """
    events = normalize_pi_event(
        {"type": event_type},
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    codes = {e.data.get("code") for e in events}
    assert "unknown_pi_event" not in codes


def test_unknown_event_is_reported_rather_than_swallowed():
    events = normalize_pi_event(
        {"type": "brand_new_event"},
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    assert [e.type for e in events] == [HarnessEventType.RUNTIME_WARNING]
    assert events[0].data["code"] == "unknown_pi_event"


def test_agent_end_is_not_a_turn_terminal():
    """``agent_end`` precedes retry, compaction, and queued continuations.

    Pi's own documented Python client breaks its read loop here. Treating it as
    terminal finalizes a Puffo turn while the model is still running.
    """
    events = normalize_pi_event(
        {"type": "agent_end", "willRetry": True},
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    assert HarnessEventType.TURN_COMPLETED not in {e.type for e in events}
    assert events[0].data == {"record_type": "agent_end", "will_retry": True}


def test_pi_turn_events_do_not_become_puffo_turn_boundaries():
    """One Puffo turn contains many Pi turns; each is not a boundary."""
    for event_type in ("turn_start", "turn_end"):
        types = {
            e.type
            for e in normalize_pi_event(
                {"type": event_type},
                session_ref=SessionRef("s"),
                turn_ref=TurnRef("t"),
            )
        }
        assert not types & {
            HarnessEventType.TURN_STARTED,
            HarnessEventType.TURN_COMPLETED,
        }


def test_queue_update_reports_counts_not_queued_message_text():
    events = normalize_pi_event(
        {
            "type": "queue_update",
            "steering": ["focus on error handling"],
            "followUp": ["then summarize"],
        },
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    data = events[0].data
    assert data["steering"] == 1 and data["follow_up"] == 1
    assert "focus on error handling" not in json.dumps(data)


def test_provider_error_text_is_classified_not_copied():
    """Retry frames carry raw provider text; only the classification escapes."""
    events = normalize_pi_event(
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 2000,
            "errorMessage": '529 {"error":{"type":"overloaded_error"}}',
        },
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    rendered = json.dumps(events[0].data)
    assert "overloaded_error" not in rendered
    assert events[0].data["failure_code"]


def test_compaction_failure_is_distinguished_from_abort():
    """``result: null`` means aborted or failed; only one carries a failure."""
    aborted = normalize_pi_event(
        {"type": "compaction_end", "result": None, "aborted": True},
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )[0].data
    failed = normalize_pi_event(
        {
            "type": "compaction_end",
            "result": None,
            "aborted": False,
            "errorMessage": "quota exceeded",
        },
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )[0].data
    assert aborted["outcome"] == "cancelled" and "failure_code" not in aborted
    assert failed["outcome"] == "failed" and failed["failure_code"]


def test_thinking_deltas_are_not_surfaced_as_assistant_output():
    for delta_type in ("thinking_start", "thinking_delta", "thinking_end"):
        events = normalize_pi_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": delta_type,
                    "delta": "secret reasoning",
                },
            },
            session_ref=SessionRef("s"),
            turn_ref=TurnRef("t"),
        )
        assert events == ()


def test_unknown_message_delta_is_reported():
    events = normalize_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "brand_new_delta"},
        },
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    assert events[0].data["code"] == "unknown_pi_message_delta"


# -- turn lifecycle over a real byte stream ---------------------------------


@pytest.mark.asyncio
async def test_open_reports_the_session_file_as_the_resume_handle():
    """``switch_session`` takes a path, so the path is the resumable identity."""
    proc = FakePiProcess()
    driver, opened = await _open(proc)
    assert opened.native_session_id == SESSION_FILE
    assert opened.resumed is False
    assert opened.capabilities.lifecycle == RuntimeLifecycle.PERSISTENT_CHILD
    await driver.close()


@pytest.mark.asyncio
async def test_open_without_session_persistence_fails_loudly():
    """A driver declaring session_resume must not quietly resume nothing."""
    proc = FakePiProcess(stats={"sessionId": "abc123"})
    driver = PiDriver(process_factory=_attesting_factory(proc))
    task = asyncio.create_task(driver.open(_spec()))
    await proc.answer_next()
    with pytest.raises(RuntimeError, match="no-session"):
        await task
    await driver.close()


@pytest.mark.asyncio
async def test_extension_vetoed_resume_is_a_failure_not_a_success():
    """``success: true`` with ``cancelled: true`` is not a resume.

    Reading only ``success`` would continue in a different session while
    reporting the requested one as restored.
    """
    proc = FakePiProcess(switch_cancelled=True)
    driver = PiDriver(process_factory=_attesting_factory(proc))
    task = asyncio.create_task(driver.open(_spec(), SessionRef(SESSION_FILE)))
    await proc.answer_next()
    with pytest.raises(Exception) as excinfo:
        await task
    assert getattr(excinfo.value, "error_code", "") == "invalid_resume"
    await driver.close()


@pytest.mark.asyncio
async def test_exactly_one_terminal_arrives_at_agent_settled():
    """A full run emits its single TURN_COMPLETED only once settled."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    turn = await started
    assert turn.native_turn_id == turn.turn_ref.value

    for frame in (
        {"type": "agent_start"},
        {"type": "turn_start"},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "hi",
            },
        },
        {"type": "turn_end"},
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ):
        proc.push(frame)

    events = await _drain_events(driver, 6)
    terminals = [e for e in events if e.type == HarnessEventType.TURN_COMPLETED]
    assert len(terminals) == 1
    assert terminals[0].turn_ref == turn.turn_ref
    assert terminals[0].native_turn_id == turn.native_turn_id
    assert terminals[0].data["outcome"] == "succeeded"
    # The terminal is last: nothing after agent_end pre-empted it.
    assert events[-1].type == HarnessEventType.TURN_COMPLETED
    await driver.close()


@pytest.mark.asyncio
async def test_final_retry_failure_marks_the_settled_turn_failed():
    """Pi reports the failure separately and settles normally afterwards."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    await started

    proc.push({"type": "agent_start"})
    proc.push({"type": "auto_retry_end", "success": False, "attempt": 3,
               "finalError": "529 overloaded"})
    proc.push({"type": "agent_settled"})

    events = await _drain_events(driver, 3)
    terminal = events[-1]
    assert terminal.type == HarnessEventType.TURN_COMPLETED
    assert terminal.data["outcome"] == "failed"
    await driver.close()


@pytest.mark.asyncio
async def test_settled_run_without_an_active_turn_is_reported_autonomous():
    """A model wake the daemon did not start must not be dropped."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push({"type": "agent_settled"})
    events = await _drain_events(driver, 1)
    assert events[0].type == HarnessEventType.AUTONOMOUS_COMPLETED
    await driver.close()


@pytest.mark.asyncio
async def test_autonomous_run_has_a_correlatable_native_turn():
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push({"type": "agent_start"})
    proc.push({
        "type": "tool_execution_start",
        "toolCallId": "call-1",
        "toolName": "send_message",
    })
    proc.push({"type": "agent_settled"})

    events = await _drain_events(driver, 3)
    started, tool, completed = events
    assert started.type == HarnessEventType.TURN_STARTED
    assert started.turn_ref is not None
    assert started.native_turn_id == started.turn_ref.value
    assert tool.type == HarnessEventType.TOOL_STARTED
    assert tool.native_turn_id == started.native_turn_id
    assert completed.type == HarnessEventType.TURN_COMPLETED
    assert completed.native_turn_id == started.native_turn_id
    await driver.close()


@pytest.mark.asyncio
async def test_two_held_replies_survive_a_pi_subturn_boundary():
    """Two competitive replies stay correlated across Pi's run boundary.

    ``agent_end`` may be followed by another ``agent_start`` before the one
    Puffo turn settles. This is the production race where both held sends
    previously failed registration because Pi exposed no native turn id.
    """
    proc = FakePiProcess()
    driver = PiDriver(
        process_factory=_attesting_factory(proc), bridge_ready_timeout=1.0
    )
    manager = RuntimeManager(driver, _spec(task_timeout_seconds=2))
    adapter = RuntimeManagerAdapter(manager)

    opening = asyncio.create_task(manager.open())
    await proc.answer_next()
    await opening

    turn_task = asyncio.create_task(adapter.run_turn(TurnContext(
        system_prompt="contract",
        messages=[{"role": "user", "content": "two inbound replies"}],
    )))
    await proc.answer_next()
    for _ in range(500):
        if manager.native_turn_id:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("Pi turn never exposed a native turn id")

    admitted: list[str] = []

    async def record_admission(event):
        admitted.append(event.planning_cycle_key)

    replies = (
        ("reply-a", "first competing reply", "receipt-a"),
        ("reply-b", "second competing reply", "receipt-b"),
    )
    for cycle, text, receipt in replies:
        adapter.register_continuation_callback(
            record_admission,
            cycle,
            channel_id="ch-race",
            tool_names=("send_message",),
            tool_arguments={"channel": "ch-race", "text": text},
            correlation_receipt=receipt,
    )

    def held_result_frame(index: int) -> dict:
        _, text, receipt = replies[index]
        return {
            "type": "tool_execution_end",
            "toolCallId": f"call-{index}",
            "toolName": "mcp__puffo__send_message",
            "isError": False,
            "_puffo_internal": "tool_result",
            "tool_call_id": f"call-{index}",
            "tool_name": "send_message",
            "arguments": {"channel": "ch-race", "text": text},
            "result": (
                f'[send_result state="held"] '
                f'[puffo:model-visible-read:{receipt}]'
            ),
        }

    proc.push({"type": "agent_start"})
    proc.push(held_result_frame(0))
    proc.push({"type": "agent_end", "willRetry": False})
    proc.push({"type": "agent_start"})
    proc.push(held_result_frame(1))
    proc.push({"type": "agent_settled"})

    await proc.answer_next()
    result = await asyncio.wait_for(turn_task, timeout=2)
    assert result.reply == ""
    assert admitted == ["reply-a", "reply-b"]
    assert manager.active_turn_ref is None
    assert manager.native_turn_id == ""
    assert not manager._continuation_admissions
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cancel_does_not_emit_its_own_terminal():
    """abort is a command; the run still terminates via agent_settled."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    turn = await started

    cancelling = asyncio.create_task(driver.cancel_turn(turn.turn_ref))
    await proc.answer_next()
    assert (await cancelling).accepted is True

    proc.push({"type": "agent_settled"})
    events = await _drain_events(driver, 1)
    assert [e.type for e in events] == [HarnessEventType.TURN_COMPLETED]
    assert events[0].data["outcome"] == "cancelled"
    await driver.close()


@pytest.mark.asyncio
async def test_steer_is_reported_as_gated_delivery():
    """Pi queues steering to the next tool boundary, so accepted != delivered."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    turn = await started

    steering = asyncio.create_task(
        driver.steer_turn(turn.turn_ref, TurnInput("also do this"))
    )
    sent = await proc.answer_next()
    receipt = await steering
    assert sent["type"] == "steer"
    assert receipt.accepted is True and receipt.delivery == "gated"
    await driver.close()


@pytest.mark.asyncio
async def test_start_turn_sends_a_bare_prompt_while_idle():
    """A streamingBehavior on an idle prompt would misdescribe the intent."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    sent = await proc.answer_next()
    await started
    assert sent["type"] == "prompt" and sent["message"] == "hello"
    assert "streamingBehavior" not in sent
    await driver.close()


# -- framing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_unicode_separator_inside_a_string_does_not_split_a_frame():
    """U+2028 is legal inside a JSON string; only LF delimits a record."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    await started

    payload = json.dumps(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "before after",
            },
        },
        ensure_ascii=False,
    )
    proc.push_raw(payload.encode() + b"\n")
    events = await _drain_events(driver, 1)
    assert events[0].type == HarnessEventType.ASSISTANT_DELTA
    assert events[0].data["delta"] == "before after"
    await driver.close()


@pytest.mark.asyncio
async def test_carriage_return_terminated_frame_is_accepted():
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push_raw(b'{"type":"agent_settled"}\r\n')
    events = await _drain_events(driver, 1)
    assert events[0].type == HarnessEventType.AUTONOMOUS_COMPLETED
    await driver.close()


@pytest.mark.asyncio
async def test_blank_records_are_framing_not_malformed_frames():
    """A blank JSONL record is separator noise, not a protocol violation.

    ``iter_jsonl_frames`` in the conformance helper already skipped these, but
    that helper is not the production reader: ``_read_loop`` handed ``b""`` to
    ``json.loads`` and reported ``protocol_parse``, telling the manager the
    peer was speaking badly when it had said nothing at all. The following
    real frame must still arrive.
    """
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push_raw(b"\n")
    proc.push_raw(b"\r\n")
    proc.push({"type": "agent_settled"})
    events = await _drain_events(driver, 1)
    assert [e.type for e in events] == [HarnessEventType.AUTONOMOUS_COMPLETED]
    await driver.close()


@pytest.mark.asyncio
async def test_malformed_frame_does_not_kill_the_reader():
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push_raw(b"not json\n")
    proc.push({"type": "agent_settled"})
    events = await _drain_events(driver, 2)
    assert events[0].data["code"] == "protocol_parse"
    assert events[1].type == HarnessEventType.AUTONOMOUS_COMPLETED
    await driver.close()


# -- extension UI sub-protocol ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(EXTENSION_UI_DIALOG_METHODS))
async def test_blocking_dialogs_are_always_answered(method):
    """An unanswered dialog stalls the Pi agent indefinitely."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    proc.push(
        {"type": "extension_ui_request", "id": "uuid-1", "method": method}
    )
    answered = await proc.answer_next()
    assert answered == {
        "type": "extension_ui_response",
        "id": "uuid-1",
        "cancelled": True,
    }
    await driver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", sorted(EXTENSION_UI_FIRE_AND_FORGET_METHODS)
)
async def test_fire_and_forget_methods_are_never_answered(method):
    """A response to one would be an id the agent is not waiting on."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    before = len(proc.stdin.frames)
    proc.push(
        {"type": "extension_ui_request", "id": "uuid-2", "method": method}
    )
    await _drain_events(driver, 1)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(proc.stdin.frames) == before
    await driver.close()


def test_every_pinned_extension_ui_method_is_classified():
    """All nine shipped methods must land in exactly one category."""
    classified = (
        EXTENSION_UI_DIALOG_METHODS | EXTENSION_UI_FIRE_AND_FORGET_METHODS
    )
    assert classified == EXTENSION_UI_REQUESTS
    assert not (
        EXTENSION_UI_DIALOG_METHODS & EXTENSION_UI_FIRE_AND_FORGET_METHODS
    )


# -- pull-only context status ------------------------------------------------


@pytest.mark.asyncio
async def test_context_status_pulls_session_stats():
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    pulling = asyncio.create_task(driver.context_status())
    await proc.answer_next()
    status = await pulling
    assert status.used_tokens == 60000
    assert status.context_window == 200000
    assert status.stale is False
    await driver.close()


@pytest.mark.asyncio
async def test_null_context_tokens_after_compaction_read_as_stale():
    """docs/rpc.md: tokens is null until a fresh post-compaction response."""
    proc = FakePiProcess(
        stats={
            "sessionFile": SESSION_FILE,
            "contextUsage": {"tokens": None, "contextWindow": 200000},
        }
    )
    driver, _ = await _open(proc)
    pulling = asyncio.create_task(driver.context_status())
    await proc.answer_next()
    status = await pulling
    assert status.used_tokens is None and status.stale is True
    await driver.close()


@pytest.mark.asyncio
async def test_permission_resolution_is_unsupported_not_silently_accepted():
    """Pi ships no permission gate; claiming to resolve one would be a lie."""
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    result = await driver.resolve_permission(
        PermissionRef("p"), PermissionDecision.APPROVE
    )
    assert isinstance(result, UnsupportedCapability)
    assert result.accepted is False
    await driver.close()


@pytest.mark.asyncio
async def test_compact_requires_an_idle_session():
    proc = FakePiProcess()
    driver, _ = await _open(proc)
    started = asyncio.create_task(driver.start_turn(TurnInput("hello")))
    await proc.answer_next()
    await started
    with pytest.raises(RuntimeError, match="idle"):
        await driver.compact(CompactRequest())
    await driver.close()


# -- tool-bridge admission ---------------------------------------------------


def test_pi_is_selectable_after_production_admission_is_opened():
    assert isinstance(build_driver("pi"), PiDriver)


def test_admission_refuses_when_the_bridge_is_missing(tmp_path):
    bare = tmp_path / "bare"
    (bare / "extensions").mkdir(parents=True)
    spec = RuntimeSpec(
        workspace_dir="/tmp/ws",
        environment={PI_AGENT_DIR_ENV: str(bare)},
    )
    with pytest.raises(PiToolBridgeUnavailableError, match="no Puffo tool bridge"):
        verify_pi_tool_bridge(spec)


def test_admission_refuses_to_fall_back_to_the_host_pi_home():
    """An unset config dir would share the host user's ~/.pi/agent."""
    spec = RuntimeSpec(workspace_dir="/tmp/ws", environment={})
    with pytest.raises(PiToolBridgeUnavailableError, match=PI_AGENT_DIR_ENV):
        verify_pi_tool_bridge(spec)


def test_admission_refuses_mcp_projection_instead_of_dropping_it():
    """Pi has no MCP slot; silently dropping servers yields a mute agent."""
    from puffo_agent.agent.harness.driver import McpServerSpec

    spec = _spec(mcp_servers=(McpServerSpec(name="puffo", command="puffo-mcp"),))
    with pytest.raises(PiToolBridgeUnavailableError, match="does not support MCP"):
        verify_pi_tool_bridge(spec)


def test_admission_accepts_a_subdirectory_bridge(tmp_path):
    home = tmp_path / "subdir-bridge"
    bridge = home / "extensions" / "puffo-tools"
    bridge.mkdir(parents=True)
    (bridge / "index.ts").write_text("// puffo tool bridge")
    spec = RuntimeSpec(
        workspace_dir="/tmp/ws",
        environment={PI_AGENT_DIR_ENV: str(home)},
    )
    assert verify_pi_tool_bridge(spec).endswith("puffo-tools/index.ts")


@pytest.mark.asyncio
async def test_open_refuses_before_spawning_a_child_without_a_bridge(tmp_path):
    """The refusal must precede the child, not surface after it is running."""
    bare = tmp_path / "bare"
    (bare / "extensions").mkdir(parents=True)
    spawned = []
    driver = PiDriver(process_factory=lambda spec: spawned.append(spec))
    spec = RuntimeSpec(
        workspace_dir="/tmp/ws",
        environment={PI_AGENT_DIR_ENV: str(bare)},
    )
    with pytest.raises(PiToolBridgeUnavailableError):
        await driver.open(spec)
    assert spawned == []
