from __future__ import annotations

import asyncio
import inspect
import json
import os
from types import SimpleNamespace

import pytest
from puffo_agent.agent.core import AgentAPIError, PuffoAgent
from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.message_store import (
    MessageStore,
    ProcessingState,
    ReceiptDisposition,
)
from puffo_agent.agent.harness import UnsupportedDriver, build_driver
from puffo_agent.agent.harness.claude_code_driver import (
    ClaudeCodeCliDriver,
    claude_capabilities,
)
from puffo_agent.agent.harness.codex_driver import (
    CODEX_CAPABILITIES,
    CodexAppServerDriver,
)
from puffo_agent.agent.harness.driver import (
    CancelReceipt,
    CompactRequest,
    Driver,
    HarnessEvent,
    PermissionDecision,
    PermissionRef,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnInput,
    TurnRef,
    TurnStarted,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
    RuntimeStateError,
    register_runtime_manager,
    unregister_runtime_manager,
)
from puffo_agent.agent.runtime_event_outbox import RuntimeEventOutbox, RuntimeEventProjectingSink
from puffo_agent.agent.runtime_events import (
    LifecycleValidator,
    RuntimeEventProjector,
)
from puffo_agent.portal.control.client import execute_command


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_name, shape", [
    ("codex", "failed_tool"),
    ("codex", "empty_assistant"),
    ("claude", "failed_tool"),
    ("claude", "empty_assistant"),
])
async def test_provider_events_project_valid_terminal(tmp_path, driver_name, shape):
    """Real provider frames must produce a valid durable terminal stream."""
    outbox = RuntimeEventOutbox(tmp_path / f"{driver_name}-{shape}.db")
    sink = RuntimeEventProjectingSink(
        outbox, RuntimeEventProjector(agent_id="agent", session_ref="session"),
    )
    turn = TurnRef(f"{driver_name}-{shape}-turn")
    if driver_name == "codex":
        driver = CodexAppServerDriver()
        driver._session_ref = SessionRef("native")
        driver._active = turn
        driver._active_native_turn_id = "native-turn"
        await driver._notification({"method": "turn/started", "params": {}})
        if shape == "failed_tool":
            await driver._notification({
                "method": "item/completed",
                "params": {"item": {
                    "id": "tool", "type": "mcpToolCall", "name": "read",
                    "status": "failed", "error": "provider failure",
                }},
            })
        else:
            await driver._notification({
                "method": "item/agentMessage/completed",
                "params": {"item": {"id": "empty", "type": "agentMessage"}},
            })
        await driver._notification({
            "method": "turn/completed",
            "params": {"turn": {"status": "failed" if shape == "failed_tool" else "completed"}},
        })
        expected_outcome = "failed" if shape == "failed_tool" else "succeeded"
    else:
        driver = ClaudeCodeCliDriver()
        driver._session_ref = SessionRef("native")
        driver._native_session_id = "native-session"
        driver._active = turn
        driver._active_native_turn_id = "replay-id"
        driver._pending_content = "start"
        driver._pending_uuid = "replay-id"
        driver._pending_replay = asyncio.get_running_loop().create_future()
        await driver._handle({
            "type": "user", "isReplay": True, "session_id": "native-session",
            "parent_tool_use_id": None, "uuid": "replay-id",
            "message": {"content": [{"type": "text", "text": "start"}]},
        })
        if shape == "failed_tool":
            await driver._handle({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tool", "name": "read", "input": {}},
            ]}})
            await driver._handle({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tool", "is_error": True, "content": "failed"},
            ]}})
        else:
            await driver._handle({"type": "assistant", "message": {"content": []}})
        await driver._handle({
            "type": "result", "subtype": "error" if shape == "failed_tool" else "success",
            "message": {},
        })
        expected_outcome = "failed" if shape == "failed_tool" else "succeeded"

    events = []
    while not driver._events.empty():
        events.append(driver._events.get_nowait())
    for value in events:
        await sink(value)
    rows = [row.event for row in outbox.prefix()]
    terminals = [row for row in rows if row["type"] == "turn.finished"]
    assert len(terminals) == 1
    assert terminals[0]["payload"]["outcome"] == expected_outcome
    if shape == "failed_tool":
        assert any(row["type"] == "tool.updated" and row["payload"].get("state") == "failed" for row in rows)
    else:
        assert not any(row["type"] == "output.updated" for row in rows)
    outbox.close()


class _FakeStdin:
    def __init__(self, on_frame=None):
        self.writes: list[bytes] = []
        self.on_frame = on_frame

    def write(self, value: bytes) -> None:
        # A write must contain exactly one complete JSONL command. This catches
        # command/request-reply byte interleaving, not merely JSON validity.
        assert value.endswith(b"\n") and value.count(b"\n") == 1
        json.loads(value)
        self.writes.append(value)
        if self.on_frame is not None:
            self.on_frame(json.loads(value))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _FakeProcess:
    def __init__(self, on_frame=None):
        self.stdout = asyncio.StreamReader()
        self.stdin = _FakeStdin(on_frame)
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.terminated = 0

    def feed(self, frame: dict) -> None:
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return int(self.returncode or 0)


async def _next_matching(stream, type_: str):
    async for value in stream:
        kind = getattr(value.type, "value", value.type)
        if kind == type_:
            return value
    raise AssertionError(f"event stream ended before {type_}")


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)


def test_contract_has_full_common_method_surface():
    expected = {
        "open", "start_turn", "steer_turn", "cancel_turn",
        "context_status", "compact", "resolve_permission", "events", "close",
    }
    assert expected <= set(dir(Driver))
    assert all(
        inspect.iscoroutinefunction(getattr(Driver, name))
        for name in expected - {"events"}
    )


def test_typed_refs_are_not_interchangeable():
    assert RuntimeRef("x") != SessionRef("x")
    assert SessionRef("x") != TurnRef("x")
    assert TurnRef("x") != PermissionRef("x")


def test_codex_effective_capabilities():
    assert CODEX_CAPABILITIES.session_resume is True
    assert CODEX_CAPABILITIES.inflight_turn_recovery is False
    assert CODEX_CAPABILITIES.steer == "current_turn"
    assert CODEX_CAPABILITIES.cancel == "typed"
    assert CODEX_CAPABILITIES.context_status == "push"
    assert CODEX_CAPABILITIES.compact == "typed"
    assert CODEX_CAPABILITIES.permission_bridge is True


def test_claude_effective_capabilities():
    baseline = claude_capabilities()
    compact = claude_capabilities(True)
    assert baseline.session_resume is True
    assert baseline.inflight_turn_recovery is False
    assert (baseline.steer, baseline.cancel) == ("none", "none")
    assert baseline.context_status == "pull"
    assert baseline.compact == "none"
    assert compact.compact == "session_command"
    assert baseline.permission_bridge is False


def test_driver_factory_is_closed_without_affecting_legacy_harness_factory():
    assert not isinstance(build_driver("codex"), UnsupportedDriver)
    assert not isinstance(build_driver("claude-code"), UnsupportedDriver)
    assert isinstance(build_driver("hermes"), UnsupportedDriver)
    assert isinstance(build_driver("gemini-cli"), UnsupportedDriver)


@pytest.mark.asyncio
async def test_encrypted_control_cancel_is_agent_scoped_and_idempotent():
    calls = []

    class Manager:
        session_ref = SessionRef("session_a")

        async def cancel_turn(self, turn):
            calls.append(turn)
            return SimpleNamespace(accepted=True)

    manager = Manager()
    register_runtime_manager("agent_a", manager)
    try:
        params = {"session_ref": "session_a", "turn_ref": "turn_a"}
        first = await execute_command(
            "runtime.cancel_turn", "agent_a", params, command_id="command_a"
        )
        second = await execute_command(
            "runtime.cancel_turn", "agent_a", params, command_id="command_a"
        )
        assert first == second == {
            "ok": True, "delivered": True, "completed": False,
        }
        assert calls == [TurnRef("turn_a")]
        rejected = await execute_command(
            "runtime.cancel_turn", "agent_other", params,
            command_id="command_other",
        )
        assert rejected["error_code"] == "runtime_unavailable"
    finally:
        unregister_runtime_manager("agent_a", manager)


@pytest.mark.asyncio
async def test_runtime_manager_one_active_turn_translates_refs_and_close_is_idempotent():
    class FakeDriver(Driver):
        def __init__(self):
            self.queue = asyncio.Queue()
            self.cancelled = []
            self.close_calls = 0
            self.resume_values = []

        async def open(self, spec, resume=None):
            self.resume_values.append(resume)
            return RuntimeOpened(
                RuntimeRef("native_runtime"), SessionRef("native_session"),
                "provider-session", False, CODEX_CAPABILITIES,
                SimpleNamespace(),
            )

        async def start_turn(self, input):
            return TurnStarted(TurnRef("driver_turn"), "native-turn")

        async def steer_turn(self, turn, input):
            return UnsupportedCapability("unused")

        async def cancel_turn(self, turn):
            self.cancelled.append(turn)
            return CancelReceipt(True, turn)

        async def context_status(self):
            return UnsupportedCapability("context_status")

        async def compact(self, request):
            return UnsupportedCapability("compact")

        async def resolve_permission(self, request, decision):
            return UnsupportedCapability("permission")

        def events(self):
            async def iterate():
                while True:
                    value = await self.queue.get()
                    if value is None:
                        return
                    yield value
            return iterate()

        async def close(self):
            self.close_calls += 1
            await self.queue.put(None)

    driver = FakeDriver()
    manager = RuntimeManager(
        driver, RuntimeSpec("/tmp"), session_ref=SessionRef("logical_session")
    )
    await manager.open()
    assert driver.resume_values == [None]
    started = await manager.start_turn(TurnInput("hello"))
    assert started.turn_ref != TurnRef("driver_turn")
    with pytest.raises(RuntimeStateError, match="one provider turn"):
        await manager.start_turn(TurnInput("overlap"))
    receipt = await manager.cancel_turn(started.turn_ref)
    assert receipt.turn_ref == started.turn_ref
    assert driver.cancelled == [TurnRef("driver_turn")]
    await driver.queue.put(HarnessEvent(
        type="turn.completed", driver="fake",
        session_ref=SessionRef("native_session"),
        turn_ref=TurnRef("driver_turn"), data={"outcome": "cancelled"},
    ))
    terminal = await manager.wait_terminal(started.turn_ref)
    assert terminal.turn_ref == started.turn_ref
    await manager.close()
    await manager.close()
    assert driver.close_calls == 1


class _ToolResultDriver(Driver):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.turn = TurnRef("driver-turn")

    async def open(self, spec, resume=None):
        return RuntimeOpened(
            RuntimeRef("runtime"), SessionRef("native-session"),
            "native-session", False, CODEX_CAPABILITIES, SimpleNamespace(),
        )

    async def start_turn(self, input):
        return TurnStarted(self.turn, "native-turn")

    async def steer_turn(self, turn, input):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn):
        return UnsupportedCapability("cancel")

    async def context_status(self):
        return UnsupportedCapability("context_status")

    async def compact(self, request):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request, decision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                yield await self.queue.get()
        return iterate()

    async def close(self):
        return None


def _register_tool_admission(adapter, admitted, key, arguments, receipt):
    async def admit(event):
        admitted.append(event)

    adapter.register_continuation_callback(
        admit, key, tool_names=("read_inbox",), tool_arguments=arguments,
        correlation_receipt=receipt,
    )


async def _emit_tool_result(
    driver, call_id, arguments, result, *, session="native-session", omitted=False,
    tool_name="read_inbox",
):
    payload = {
        "_puffo_internal": "tool_result", "tool_call_id": call_id,
        "tool_name": tool_name, "arguments": arguments,
        "result": result, "is_error": False,
    }
    if omitted:
        payload["result_omitted"] = True
    await driver.queue.put(HarnessEvent.normalized(
        type="turn.tool_completed", driver="fake",
        session_ref=SessionRef(session), turn_ref=driver.turn,
        native_session_id=session, native_turn_id="native-turn",
        data={
            "tool_call_ref": call_id, "label": "read_inbox",
            "outcome": "succeeded",
        },
        native_payload=payload,
    ))


async def _wait_for_admitted(admitted):
    for _ in range(20):
        if admitted:
            return
        await asyncio.sleep(0)


async def _assert_tool_result_correlations(adapter, driver, admitted):
    _register_tool_admission(
        adapter, admitted, "read-page-1", {"limit": 1}, "receipt-1",
    )
    await _emit_tool_result(
        driver, "call-1", {"limit": 1}, "[puffo:model-visible-read:receipt-1]",
        tool_name="mcp__puffo__read_inbox",
    )
    await _wait_for_admitted(admitted)
    assert len(admitted) == 1
    assert admitted[0].planning_cycle_key == "read-page-1"
    assert admitted[0].provider_session_id == "native-session"
    assert admitted[0].provider_turn_id == "native-turn"
    assert admitted[0].tool_call_id == "call-1"

    admitted.clear()
    _register_tool_admission(
        adapter, admitted, "read-page-without-result",
        {"cursor": "next"}, "receipt-omitted",
    )
    await _emit_tool_result(driver, "call-2", {"cursor": "next"}, None)
    await _wait_for_admitted(admitted)
    assert admitted == []

    _register_tool_admission(
        adapter, admitted, "read-page-provider-omitted-result",
        {"cursor": "provider-omitted"}, "receipt-provider-omitted",
    )
    await _emit_tool_result(
        driver, "call-provider-omitted", {"cursor": "provider-omitted"},
        None, omitted=True,
    )
    await _wait_for_admitted(admitted)
    assert len(admitted) == 1
    assert admitted[0].planning_cycle_key == "read-page-provider-omitted-result"
    assert admitted[0].tool_call_id == "call-provider-omitted"


async def _assert_ambiguous_and_foreign_tool_results(adapter, driver, admitted):
    admitted.clear()
    for cycle in ("ambiguous-a", "ambiguous-b"):
        _register_tool_admission(
            adapter, admitted, cycle, {"cursor": "ambiguous"}, f"receipt-{cycle}",
        )
    await _emit_tool_result(
        driver, "call-ambiguous", {"cursor": "ambiguous"}, None, omitted=True,
    )
    await asyncio.sleep(0)
    assert admitted == []
    _register_tool_admission(
        adapter, admitted, "read-page-foreign-session",
        {"cursor": "foreign"}, "receipt-2",
    )
    marker = "[puffo:model-visible-read:receipt-2]"
    await _emit_tool_result(
        driver, "call-foreign", {"cursor": "foreign"}, marker,
        session="foreign-session",
    )
    await asyncio.sleep(0)
    assert admitted == []
    await _emit_tool_result(driver, "call-2", {"cursor": "foreign"}, marker)
    await _wait_for_admitted(admitted)
    assert len(admitted) == 1
    assert admitted[0].planning_cycle_key == "read-page-foreign-session"
    assert admitted[0].tool_call_id == "call-2"


async def _assert_cancelled_manager_adapter_fails():
    class FailedManager:
        async def start_turn(self, input):
            return TurnStarted(TurnRef("logical-turn"), "native-turn")

        def events(self):
            async def iterate():
                yield HarnessEvent(
                    type="turn.completed", driver="fake",
                    session_ref=SessionRef("logical-session"),
                    turn_ref=TurnRef("logical-turn"), data={"outcome": "cancelled"},
                )
            return iterate()

    with pytest.raises(RuntimeStateError, match="outcome cancelled"):
        await RuntimeManagerAdapter(FailedManager()).run_turn(SimpleNamespace(
            messages=[{"role": "user", "content": "notice"}], on_progress=None,
        ))


@pytest.mark.asyncio
async def test_runtime_manager_correlates_private_tool_result_and_rejects_terminal_failure():
    driver = _ToolResultDriver()
    manager = RuntimeManager(driver, RuntimeSpec("/tmp"))
    adapter = RuntimeManagerAdapter(manager)
    await manager.open()
    started = await manager.start_turn(TurnInput("notice"))
    admitted = []

    await _assert_tool_result_correlations(adapter, driver, admitted)
    await _assert_ambiguous_and_foreign_tool_results(adapter, driver, admitted)

    await driver.queue.put(HarnessEvent(
        type="turn.completed", driver="fake",
        session_ref=SessionRef("native-session"), turn_ref=driver.turn,
        native_session_id="native-session", native_turn_id="native-turn",
        data={"outcome": "failed"},
    ))
    terminal = await manager.wait_terminal(started.turn_ref)
    assert terminal.data["outcome"] == "failed"
    await manager.close()
    await _assert_cancelled_manager_adapter_fails()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "outcome", "succeeds"),
    [
        ("turn.completed", "succeeded", True),
        ("turn.completed", "failed", False),
        ("turn.completed", "cancelled", False),
        ("turn.abandoned", "abandoned", False),
    ],
)
async def test_manager_adapter_only_accepts_succeeded_canonical_terminal(
    event_type, outcome, succeeds,
):
    class TerminalManager:
        async def start_turn(self, input):
            return TurnStarted(TurnRef("logical-turn"), "native-turn")

        def events(self):
            async def iterate():
                yield HarnessEvent(
                    type=event_type,
                    driver="fake",
                    session_ref=SessionRef("logical-session"),
                    turn_ref=TurnRef("logical-turn"),
                    data={"outcome": outcome},
                )
            return iterate()

    call = RuntimeManagerAdapter(TerminalManager()).run_turn(
        SimpleNamespace(
            messages=[{"role": "user", "content": "notice"}],
            on_progress=None,
        )
    )
    if succeeds:
        assert (await call).metadata["turn_ref"] == "logical-turn"
    else:
        with pytest.raises(RuntimeStateError, match=f"outcome {outcome}"):
            await call


@pytest.mark.asyncio
async def test_manager_adapter_submits_only_current_semantic_input():
    """A resumed provider session must not receive historical notice text."""
    historical_notice = (
        "<global_inbox_notice>historical-notice-sentinel</global_inbox_notice>"
    )
    current_notice = (
        "<global_inbox_notice>current-notice-sentinel</global_inbox_notice>"
    )
    captured = {}

    class Manager:
        async def start_turn(self, input):
            captured["content"] = input.content
            return TurnStarted(TurnRef("logical-turn"), "native-turn")

        def events(self):
            async def iterate():
                yield HarnessEvent(
                    type="turn.completed",
                    driver="fake",
                    session_ref=SessionRef("logical-session"),
                    turn_ref=TurnRef("logical-turn"),
                    data={"outcome": "succeeded"},
                )
            return iterate()

    await RuntimeManagerAdapter(Manager()).run_turn(SimpleNamespace(
        messages=[
            {"role": "user", "content": historical_notice},
            {"role": "assistant", "content": "already handled"},
            {"role": "user", "content": current_notice},
        ],
        on_progress=None,
    ))

    assert captured["content"] == current_notice
    assert "historical-notice-sentinel" not in captured["content"]
    assert captured["content"].count("current-notice-sentinel") == 1


def _claude_context_response(frame, total_tokens=42, max_tokens=200_000):
    return {
        "type": "control_response",
        "response": {
            "request_id": frame["request_id"],
            "subtype": "success",
            "response": {
                "totalTokens": total_tokens, "rawMaxTokens": max_tokens,
            },
        },
    }


def _metadata_driver(provider, provider_inputs):
    holder = {}

    def on_codex_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {"server": "fake"}})
        elif method == "thread/start":
            proc.feed({
                "id": frame["id"], "result": {"thread": {"id": "native-session"}},
            })
        elif method == "turn/start":
            provider_inputs.append(frame["params"]["input"][0]["text"])
            proc.feed({
                "id": frame["id"], "result": {"turn": {"id": "native-turn"}},
            })

    if provider == "codex":
        proc = _FakeProcess(on_codex_frame)
        holder["proc"] = proc
        return proc, CodexAppServerDriver(lambda _spec: proc)

    def replay_claude_frame(frame):
        if frame.get("type") == "control_request":
            proc.feed(_claude_context_response(frame, total_tokens=1))
            return
        if frame.get("type") == "user":
            provider_inputs.append(frame["message"]["content"][0]["text"])
            proc.feed({**frame, "isReplay": True})

    proc = _FakeProcess(replay_claude_frame)
    proc.feed({
        "type": "system", "subtype": "init",
        "session_id": "native-session", "slash_commands": [],
    })
    return proc, ClaudeCodeCliDriver(lambda *_args: proc, replay_timeout=0.5)


async def _metadata_store(tmp_path):
    store = MessageStore(tmp_path / "messages.db", now_ms=lambda: 1_000)
    await store.open()
    for seq in (1, 2):
        await store.store_receipt(
            {
                "envelope_id": f"message-{seq}", "envelope_kind": "channel",
                "sender_slug": "alice", "channel_id": "channel",
                "space_id": "space", "content": f"secret-{seq}",
                "content_type": "text/plain", "sent_at": seq,
                "is_encrypted": True,
            },
            server_seq=seq, disposition=ReceiptDisposition.ELIGIBLE,
            reason="test", received_at=1_000,
        )
        assert (await store.get_notice_state()).first_pending_deadline_ms == 4_000
    assert (await store.get_notice_state()).pending_count == 2
    return store


async def _metadata_runtime(tmp_path, driver, store):
    manager = RuntimeManager(
        driver, RuntimeSpec(str(tmp_path)),
        session_ref=SessionRef("logical-session"),
    )
    adapter = RuntimeManagerAdapter(manager)
    await adapter.warm("system")
    agent = PuffoAgent(
        adapter, "system", str(tmp_path / "memory"),
        workspace_dir=str(tmp_path), agent_id="agent",
    )
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter,
        run_turn=agent.handle_global_inbox_turn, workspace=tmp_path,
    )
    expected = await runtime.plan_pending()
    assert expected is not None
    return adapter, agent, runtime, expected.provider_input, asyncio.create_task(
        runtime.process_once()
    )


async def _wait_metadata_turn(runtime, task, provider_inputs, expected, agent):
    for _ in range(500):
        if runtime.active.turn_id:
            break
        if task.done():
            await task
        await asyncio.sleep(0.001)
    assert runtime.active.provider_session_id == "native-session"
    assert runtime.active.message_ids == []
    assert provider_inputs == [expected]
    assert all("<global_inbox_notice>" not in entry["content"] for entry in agent.log)


def _feed_metadata_result(proc, provider, page_number, arguments, marker):
    tool_id = f"tool-{page_number}"
    if provider == "codex":
        item = {
            "id": tool_id, "type": "mcpToolCall", "tool": "read_inbox",
            "arguments": arguments, "result": marker,
        }
        if page_number == 2:
            item.update({
                "type": "dynamicToolCall", "status": "completed",
                "success": True, "contentItems": None,
            })
            item.pop("result")
        proc.feed({"method": "item/completed", "params": {"item": item}})
        return
    proc.feed({
        "type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": tool_id, "name": "read_inbox",
            "input": arguments,
        }]},
    })
    proc.feed({
        "type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": tool_id, "content": marker,
        }]},
    })


async def _read_metadata_pages(runtime, proc, provider):
    cursor, admitted = "", []
    for page_number in (1, 2):
        arguments = {"cursor": cursor, "limit": 1}
        page = await runtime.read_inbox(
            cursor=cursor, limit=1, tool_arguments=arguments,
        )
        assert len(page["messages"]) == 1
        _feed_metadata_result(proc, provider, page_number, arguments, "read complete")
        admitted.append(f"message-{page_number}")

        def admission_is_persisted():
            if runtime.active.message_ids != admitted:
                return False
            try:
                persisted = json.loads(runtime.current_turn_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return False
            return persisted.get("message_ids") == admitted

        await _wait_until(admission_is_persisted)
        assert runtime.active.message_ids == admitted
        persisted = json.loads(runtime.current_turn_path.read_text())
        assert persisted["message_ids"] == admitted
        assert persisted["provider_session_id"] == "native-session"
        cursor = page["next_cursor"]


def _finish_metadata_turn(proc, provider, outcome):
    if provider == "codex":
        status = "completed" if outcome == "succeeded" else "failed"
        proc.feed({"method": "turn/completed", "params": {"turn": {"status": status}}})
    else:
        subtype = "success" if outcome == "succeeded" else "error"
        proc.feed({"type": "result", "subtype": subtype, "usage": {}})


async def _assert_metadata_terminal(store, runtime, task, outcome):
    assert await asyncio.wait_for(task, timeout=1)
    states = [
        (await store.get_message_by_envelope(f"message-{seq}")).processing_state
        for seq in (1, 2)
    ]
    expected = (
        [ProcessingState.PROCESSED, ProcessingState.PROCESSED]
        if outcome == "succeeded"
        else [ProcessingState.PENDING, ProcessingState.PENDING]
    )
    assert states == expected
    assert not runtime.current_turn_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude-code"])
@pytest.mark.parametrize("terminal_outcome", ["succeeded", "failed"])
async def test_metadata_notice_through_real_manager_reads_paginated_exact_union(
    tmp_path, provider, terminal_outcome,
):
    provider_inputs = []
    proc, driver = _metadata_driver(provider, provider_inputs)

    store = await _metadata_store(tmp_path)
    adapter, agent, runtime, expected, task = await _metadata_runtime(
        tmp_path, driver, store,
    )
    await _wait_metadata_turn(runtime, task, provider_inputs, expected, agent)

    await _read_metadata_pages(runtime, proc, provider)

    _finish_metadata_turn(proc, provider, terminal_outcome)
    await _assert_metadata_terminal(store, runtime, task, terminal_outcome)
    await adapter.aclose()
    await store.close()


def _codex_protocol_driver():
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {"server": "fake"}})
        elif method == "thread/start":
            proc.feed({"id": frame["id"], "result": {"thread": {"id": "th_1"}}})
        elif method == "turn/start":
            assert frame["params"]["clientUserMessageId"] == "client-1"
            proc.feed({"id": frame["id"], "result": {"turn": {"id": "native-1"}}})
        elif method in {"turn/steer", "turn/interrupt"}:
            assert frame["params"].get("expectedTurnId", "native-1") == "native-1"
            proc.feed({"id": frame["id"], "result": {}})
        elif method == "thread/compact/start":
            proc.feed({"id": frame["id"], "result": {"id": "compact-1"}})

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    return proc, CodexAppServerDriver(lambda _spec: proc)


async def _open_codex_protocol_driver(proc, driver):
    opened = await driver.open(RuntimeSpec("/workspace", model="gpt"))
    assert opened.native_session_id == "th_1"
    methods = [json.loads(value).get("method") for value in proc.stdin.writes]
    assert methods[:3] == ["initialize", "initialized", "thread/start"]
    stream = driver.events()
    await _next_matching(stream, "session.opened")
    started = await driver.start_turn(
        TurnInput("hello", client_correlation_id="client-1")
    )
    return stream, started


def _feed_codex_protocol_events(proc):
    proc.feed({"method": "turn/started", "params": {"turn": {"id": "native-1"}}})
    proc.feed({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {
            "last": {"totalTokens": 12},
            "total": {},
            "modelContextWindow": 100,
        }},
    })
    proc.feed({
        "id": 900, "method": "item/commandExecution/requestApproval",
        "params": {"turnId": "native-1", "command": "SECRET"},
    })
    proc.feed({"method": "future/notification", "params": {"secret": "SECRET"}})


async def _read_codex_protocol_events(stream):
    await _next_matching(stream, "turn.started")
    await _next_matching(stream, "turn.context_updated")
    permission = await _next_matching(stream, "turn.permission_requested")
    warning = await _next_matching(stream, "runtime.warning")
    assert warning.data == {
        "code": "unknown_notification", "method": "future/notification",
    }
    return permission


async def _exercise_codex_protocol_controls(driver, started, permission, stream):
    await asyncio.gather(
        driver.steer_turn(started.turn_ref, TurnInput("more")),
        driver.cancel_turn(started.turn_ref),
        driver.resolve_permission(
            PermissionRef(permission.data["permission_ref"]),
            PermissionDecision.APPROVE,
        ),
    )
    permission_updated = await _next_matching(stream, "turn.permission_updated")
    assert permission_updated.data["state"] == "approved"


async def _assert_codex_mcp_tool_event(proc, stream):
    proc.feed({
        "method": "item/completed",
        "params": {"item": {
            "id": "tool-1", "type": "mcpToolCall",
            "tool": "read_inbox", "arguments": {"limit": 1},
            "result": "[puffo:model-visible-read:receipt-1]",
        }},
    })
    completed = await _next_matching(stream, "turn.tool_completed")
    assert completed.data == {
        "tool_call_ref": "tool-1",
        "label": "read_inbox",
        "outcome": "succeeded",
    }
    assert "arguments" not in completed.data
    assert "result" not in completed.data


async def _assert_codex_dynamic_tool_event(proc, stream):
    proc.feed({
        "method": "item/completed",
        "params": {"item": {
            "id": "tool-2", "type": "dynamicToolCall",
            "namespace": "mcp__puffo", "tool": "read_inbox",
            "status": "completed", "success": True,
            "arguments": {"target": "", "limit": 50},
            # Current live Codex may omit contentItems even though the model
            # received the tool output.
            "contentItems": None,
        }},
    })
    completed = await _next_matching(stream, "turn.tool_completed")
    assert completed.data == {
        "tool_call_ref": "tool-2",
        "label": "read_inbox",
        "outcome": "succeeded",
    }


async def _assert_codex_protocol_completion(proc, driver, stream):
    decoded = [json.loads(value) for value in proc.stdin.writes]
    assert any(value.get("method") == "turn/steer" for value in decoded)
    assert any(value.get("method") == "turn/interrupt" for value in decoded)
    assert any(
        value.get("id") == 900
        and value.get("result") == {"decision": "accept"}
        for value in decoded
    )
    assert (await driver.context_status()).used_tokens == 12
    proc.feed({
        "id": 901,
        "method": "item/commandExecution/requestApproval",
        "params": {"turnId": "native-1", "command": "pending"},
    })
    await _next_matching(stream, "turn.permission_requested")
    assert driver._permission_requests
    proc.feed({
        "method": "turn/completed",
        "params": {"turn": {"status": "interrupted"}},
    })
    await _next_matching(stream, "turn.completed")
    assert not driver._permission_requests
    compact = await driver.compact(CompactRequest())
    assert compact.operation_ref == "compact-1"
    await driver.close()
    await driver.close()
    assert proc.terminated == 1


@pytest.mark.asyncio
async def test_codex_driver_fake_app_server_full_protocol_and_concurrency():
    proc, driver = _codex_protocol_driver()
    stream, started = await _open_codex_protocol_driver(proc, driver)
    _feed_codex_protocol_events(proc)
    permission = await _read_codex_protocol_events(stream)
    await _exercise_codex_protocol_controls(driver, started, permission, stream)
    await _assert_codex_mcp_tool_event(proc, stream)
    await _assert_codex_dynamic_tool_event(proc, stream)
    await _assert_codex_protocol_completion(proc, driver, stream)


def _feed_codex_started_items(proc):
    proc.feed({
        "method": "item/started",
        "params": {"item": {
            "id": "tool-9", "type": "mcpToolCall",
            "name": "mcp__puffo__read_inbox", "arguments": {"limit": 1},
        }},
    })
    for method in ("item/started", "item/completed"):
        proc.feed({
            "method": method,
            "params": {"item": {"id": "cmp-1", "type": "contextCompaction"}},
        })
    # An unrecognised started item is known-method noise, not an unknown
    # notification.
    proc.feed({
        "method": "item/started",
        "params": {"item": {"id": "x", "type": "enteredReviewMode"}},
    })
    proc.feed({"method": "turn/completed", "params": {"turn": {}}})


async def _collect_codex_notification_events(stream):
    collected = []
    async for value in stream:
        collected.append(value)
        kind = getattr(value.type, "value", value.type)
        if kind == "turn.completed":
            return collected
    raise AssertionError("event stream ended before the turn finished")


@pytest.mark.asyncio
async def test_codex_driver_normalizes_started_tool_and_compaction_items():
    proc, driver = _codex_protocol_driver()
    stream, _started = await _open_codex_protocol_driver(proc, driver)
    _feed_codex_started_items(proc)

    events = await _collect_codex_notification_events(stream)
    kinds = [getattr(value.type, "value", value.type) for value in events]
    assert kinds == [
        "turn.tool_started",
        "compaction.started",
        "compaction.completed",
        "turn.completed",
    ]
    assert events[0].data == {"tool_call_ref": "tool-9", "label": "read_inbox"}
    # A compaction item is not an output block, so it must not close one.
    assert events[2].data == {}
    await driver.close()


@pytest.mark.asyncio
async def test_claude_driver_emits_compaction_boundary_and_clears_tool_calls():
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        if frame.get("type") != "user" or not frame.get("uuid"):
            return
        proc.feed({**frame, "isReplay": True})
        proc.feed({"type": "system", "subtype": "status", "status": "compacting"})
        proc.feed({"type": "system", "subtype": "compact_boundary"})
        proc.feed({
            "type": "assistant",
            "uuid": "frame-uuid-1",
            "message": {"content": [
                {"type": "tool_use", "id": "call-1", "name": "read_inbox",
                 "input": {"limit": 1}},
            ]},
        })
        # No matching `tool_result`: the turn ends with the call outstanding.
        proc.feed({"type": "result", "subtype": "success", "usage": {}})

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    proc.feed({
        "type": "system", "subtype": "init",
        "session_id": "claude-1", "slash_commands": [],
    })
    driver = ClaudeCodeCliDriver(lambda _args, _spec: proc, replay_timeout=1)
    await driver.open(RuntimeSpec("/workspace"))
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))

    assert (await _next_matching(stream, "compaction.started")) is not None
    assert (await _next_matching(stream, "compaction.completed")) is not None
    await _next_matching(stream, "turn.completed")
    assert driver._tool_calls == {}
    await driver.close()


@pytest.mark.asyncio
async def test_codex_driver_resumes_with_native_session_id_after_handshake():
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        if frame.get("method") == "initialize":
            proc.feed({"id": frame["id"], "result": {}})
        elif frame.get("method") == "thread/resume":
            assert frame["params"] == {
                "threadId": "native-thread",
                "cwd": "/workspace",
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": "gpt",
            }
            proc.feed({
                "id": frame["id"],
                "result": {"thread": {"id": "native-thread"}},
            })

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    driver = CodexAppServerDriver(lambda _spec: proc)
    opened = await driver.open(
        RuntimeSpec("/workspace", model="gpt"), SessionRef("native-thread")
    )
    assert opened.resumed and opened.native_session_id == "native-thread"
    assert [
        json.loads(value).get("method") for value in proc.stdin.writes
    ] == ["initialize", "initialized", "thread/resume"]
    await driver.close()


@pytest.mark.asyncio
async def test_codex_driver_reopens_cleanly_after_closing_an_active_turn():
    processes = []

    def factory(_spec):
        holder = {}

        def on_frame(frame):
            proc = holder["proc"]
            if frame.get("method") == "initialize":
                proc.feed({"id": frame["id"], "result": {}})
            elif frame.get("method") == "thread/start":
                proc.feed({
                    "id": frame["id"],
                    "result": {"thread": {"id": f"thread-{len(processes)}"}},
                })
            elif frame.get("method") == "turn/start":
                proc.feed({
                    "id": frame["id"],
                    "result": {"turn": {"id": f"turn-{len(processes)}"}},
                })

        proc = _FakeProcess(on_frame)
        holder["proc"] = proc
        processes.append(proc)
        return proc

    driver = CodexAppServerDriver(factory)
    await driver.open(RuntimeSpec("/workspace"))
    first = await driver.start_turn(TurnInput("first"))
    assert first.accepted

    await driver.close()
    assert not driver._pending
    assert not driver._active.value
    assert not driver._active_native_turn_id
    await driver.open(RuntimeSpec("/workspace"))
    second = await driver.start_turn(TurnInput("second"))

    assert second.accepted
    assert len(processes) == 2
    await driver.close()


@pytest.mark.asyncio
async def test_codex_driver_bounds_a_silent_turn_start_with_a_retryable_error():
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {}})
        elif method == "thread/start":
            proc.feed({"id": frame["id"], "result": {"thread": {"id": "th_1"}}})
        # turn/start is deliberately never answered: provider silence must not
        # leave the Agent turn active forever.

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    driver = CodexAppServerDriver(
        lambda _spec: proc, request_timeout_seconds=0.05
    )
    await driver.open(RuntimeSpec("/workspace"))

    with pytest.raises(AgentAPIError) as raised:
        await driver.start_turn(TurnInput("hello"))

    assert raised.value.is_auth is False
    assert "turn/start" in str(raised.value)
    assert not driver._pending
    assert not driver._active.value
    await driver.close()


@pytest.mark.asyncio
async def test_codex_driver_bounds_a_silent_startup_handshake():
    proc = _FakeProcess()
    driver = CodexAppServerDriver(
        lambda _spec: proc, request_timeout_seconds=0.05
    )

    with pytest.raises(AgentAPIError) as raised:
        await driver.open(RuntimeSpec("/workspace"))

    assert raised.value.is_auth is False
    assert "initialize" in str(raised.value)
    assert not driver._pending
    await driver.close()


@pytest.mark.asyncio
async def test_codex_subprocess_stdout_matches_the_claude_frame_limit(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    driver = CodexAppServerDriver(request_timeout_seconds=0.05)

    with pytest.raises(AgentAPIError):
        await driver.open(RuntimeSpec("/workspace"))

    assert captured["limit"] == 16 * 1024 * 1024
    await driver.close()


@pytest.mark.asyncio
async def test_claude_driver_reopens_cleanly_after_closing_an_active_turn():
    processes = []

    def factory(_args, _spec):
        holder = {}

        def on_frame(frame):
            if frame.get("type") == "user" and frame.get("uuid"):
                holder["proc"].feed({**frame, "isReplay": True})

        proc = _FakeProcess(on_frame)
        holder["proc"] = proc
        processes.append(proc)
        proc.feed({
            "type": "system",
            "subtype": "init",
            "session_id": f"claude-{len(processes)}",
            "slash_commands": [],
        })
        return proc

    driver = ClaudeCodeCliDriver(factory, replay_timeout=1)
    await driver.open(RuntimeSpec("/workspace"))
    first = await driver.start_turn(TurnInput("first"))
    assert first.accepted

    await driver.close()
    assert driver._pending_replay is None
    assert not driver._active.value
    assert not driver._active_native_turn_id
    await driver.open(RuntimeSpec("/workspace"))
    second = await driver.start_turn(TurnInput("second"))

    assert second.accepted
    assert len(processes) == 2
    await driver.close()


async def _assert_claude_unsupported_calls_write_nothing(
    driver, started, process,
):
    assert isinstance(
        await driver.cancel_turn(started.turn_ref), UnsupportedCapability
    )
    before = len(process.stdin.writes)
    assert isinstance(
        await driver.resolve_permission(
            PermissionRef("p"), PermissionDecision.DENY
        ),
        UnsupportedCapability,
    )
    assert len(process.stdin.writes) == before


@pytest.mark.asyncio
async def test_claude_driver_exact_replay_trailing_records_and_unsupported_zero_writes():
    captured_args = []
    holder = {}

    def factory(args, _spec):
        captured_args.extend(args)

        def on_frame(frame):
            proc = holder["proc"]
            if frame.get("type") == "control_request":
                proc.feed(_claude_context_response(frame))
                return
            if frame.get("uuid") == "replay-1":
                proc.feed({
                    **frame, "isReplay": True,
                    "message": {"role": "user", "content": [
                        {"type": "text", "text": "hello\nworld"}
                    ]},
                })
                proc.feed({
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "text", "text": "visible"},
                        {
                            "type": "tool_use", "id": "tool-claude",
                            "name": "read_inbox", "input": {"limit": 1},
                        },
                        ]},
                })

        proc = _FakeProcess(on_frame)
        holder["proc"] = proc
        proc.feed({
            "type": "system", "subtype": "init", "session_id": "claude-1",
            "slash_commands": ["/compact"],
        })
        return proc

    driver = ClaudeCodeCliDriver(factory, replay_timeout=1)
    opened = await driver.open(RuntimeSpec("/workspace"))
    assert "--replay-user-messages" in captured_args
    assert opened.capabilities.compact == "session_command"
    stream = driver.events()
    await _next_matching(stream, "session.opened")
    started = await driver.start_turn(TurnInput(
        "hello\r\nworld", client_correlation_id="replay-1"
    ))
    assert started.accepted and started.delivery == "stdin_written"
    assert started.replay_id == ""
    await _next_matching(stream, "turn.started")
    await _next_matching(stream, "turn.assistant_delta")
    await _next_matching(stream, "turn.tool_started")
    steered = await driver.steer_turn(started.turn_ref, TurnInput("inbox delta"))
    assert isinstance(steered, UnsupportedCapability)
    assert steered.capability == "steer"
    holder["proc"].feed({
        "type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "tool-claude",
            "content": "[puffo:model-visible-read:receipt-claude]",
        }]},
    })
    tool_completed = await _next_matching(stream, "turn.tool_completed")
    assert tool_completed.data == {
        "tool_call_ref": "tool-claude",
        "label": "read_inbox",
        "outcome": "succeeded",
    }
    assert "arguments" not in tool_completed.data
    assert "result" not in tool_completed.data
    assert len(holder["proc"].stdin.writes) == 1
    holder["proc"].feed({"type": "result", "subtype": "success", "usage": {}})
    holder["proc"].feed({"type": "rate_limit_event", "secret": "not-public"})
    await _next_matching(stream, "turn.completed")
    trailing = await _next_matching(stream, "session.updated")
    assert trailing.turn_ref is None

    context = await driver.context_status()
    assert (context.used_tokens, context.context_window, context.stale) == (
        42,
        200_000,
        False,
    )
    await _assert_claude_unsupported_calls_write_nothing(
        driver, started, holder["proc"]
    )
    assert (await driver.compact(CompactRequest("now"))).accepted
    await driver.close()


@pytest.mark.asyncio
async def test_claude_driver_accepts_init_after_first_stream_input():
    captured = []
    holder = {}

    def factory(args, _spec):
        captured.extend(args)

        def on_frame(frame):
            if frame.get("type") != "user":
                return
            holder["proc"].feed({
                "type": "system",
                "subtype": "init",
                "session_id": frame["session_id"],
                "slash_commands": ["/compact"],
            })
            holder["proc"].feed({**frame, "isReplay": True})

        proc = _FakeProcess(on_frame)
        holder["proc"] = proc
        return proc

    driver = ClaudeCodeCliDriver(factory, replay_timeout=1)
    opened = await driver.open(RuntimeSpec("/workspace"))
    session_id_index = captured.index("--session-id")
    assert captured[session_id_index + 1] == opened.native_session_id
    assert driver._init is not None and not driver._init.done()

    stream = driver.events()
    started = await driver.start_turn(TurnInput("hello"))

    assert started.accepted
    await _next_matching(stream, "session.opened")
    await _next_matching(stream, "turn.started")
    await driver.close()


@pytest.mark.asyncio
async def test_claude_driver_prepends_normalized_launch_argv(monkeypatch):
    """Regression-pin for the A1 gap: the launch argv must be the
    normalized executable prefix (the Windows wrapper block) followed by
    the untouched flags. On this host the real boundary passes the
    executable through, so the wiring + ordering both stay pinned."""
    import puffo_agent.agent.harness.claude_code_driver as driver_mod

    def make_factory(captured):
        def factory(args, _spec):
            captured.extend(args)
            proc = _FakeProcess()
            proc.feed({
                "type": "system", "subtype": "init",
                "session_id": f"claude-{len(captured)}", "slash_commands": ["/compact"],
            })
            return proc
        return factory

    posix = []
    driver = ClaudeCodeCliDriver(make_factory(posix), replay_timeout=1)
    opened = await driver.open(RuntimeSpec("/workspace", executable="claude", launch_args=("--autocompact", "100000"), auto_compact_threshold_tokens=500000))
    await driver.close()
    assert posix[:4] == ["claude", "--autocompact", "500000", "-p"] and opened.capabilities.compact == "session_command"

    monkeypatch.setattr(
        driver_mod, "normalize_launch_argv",
        lambda executable: ["cmd.exe", "/c", executable + ".cmd"],
    )
    windows = []
    driver = ClaudeCodeCliDriver(make_factory(windows), replay_timeout=1)
    await driver.open(RuntimeSpec("/workspace", executable="claude", auto_compact_threshold_tokens=500000))
    await driver.close()
    assert windows[:6] == ["cmd.exe", "/c", "claude.cmd", "--autocompact", "500000", "-p"]


@pytest.mark.asyncio
async def test_claude_driver_resume_flag_maps_to_resumed_system_init():
    captured = []

    def factory(args, _spec):
        captured.extend(args)
        proc = _FakeProcess()
        proc.feed({
            "type": "system", "subtype": "init",
            "session_id": "native-claude-session",
        })
        return proc

    driver = ClaudeCodeCliDriver(factory, replay_timeout=1)
    opened = await driver.open(
        RuntimeSpec("/workspace"), SessionRef("native-claude-session")
    )
    resume_index = captured.index("--resume")
    assert captured[resume_index + 1] == "native-claude-session"
    assert opened.resumed
    await driver.close()


@pytest.mark.asyncio
async def test_claude_driver_stdin_delivery_does_not_wait_for_replay():
    holder = {}

    def factory(_args, _spec):
        def on_frame(_frame):
            holder["proc"].stdout.feed_eof()

        proc = _FakeProcess(on_frame)
        holder["proc"] = proc
        proc.feed({
            "type": "system", "subtype": "init", "session_id": "claude-loss",
        })
        return proc

    driver = ClaudeCodeCliDriver(factory, replay_timeout=1)
    await driver.open(RuntimeSpec("/workspace"))
    receipt = await driver.start_turn(TurnInput("accepted maybe"))
    assert receipt.accepted
    assert receipt.delivery == "stdin_written"
    await driver.close()


@pytest.mark.asyncio
async def test_control_permission_stale_and_cross_agent_references_fail_closed():
    calls = []

    class Manager:
        session_ref = SessionRef("session_a")

        async def resolve_permission(self, turn, permission, decision):
            calls.append((turn, permission, decision))
            if turn != TurnRef("turn_a") or permission != PermissionRef("perm_a"):
                raise RuntimeStateError("stale")
            return SimpleNamespace(accepted=True)

    manager = Manager()
    register_runtime_manager("agent_permission", manager)
    try:
        params = {
            "session_ref": "session_a", "turn_ref": "turn_a",
            "permission_ref": "perm_a", "decision": "approved",
        }
        delivered = await execute_command(
            "runtime.resolve_permission", "agent_permission", params,
            command_id="permission-command",
        )
        assert delivered == {
            "ok": True, "delivered": True, "completed": False,
        }
        stale = await execute_command(
            "runtime.resolve_permission", "agent_permission",
            {**params, "turn_ref": "old"}, command_id="permission-stale",
        )
        assert stale["error_code"] == "stale_runtime_reference"
        cross = await execute_command(
            "runtime.resolve_permission", "different-agent", params,
            # Idempotency keys are scoped by Agent and operation; replaying a
            # valid key against a different Agent must not reuse its result.
            command_id="permission-command",
        )
        assert cross["error_code"] == "runtime_unavailable"
    finally:
        unregister_runtime_manager("agent_permission", manager)


def _two_block_claude_driver():
    """A Claude turn whose single assistant frame carries two text blocks."""
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        if frame.get("type") == "user" and frame.get("uuid"):
            proc.feed({**frame, "isReplay": True})
            proc.feed({
                "type": "assistant",
                "uuid": "frame-uuid-1",
                "message": {"content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ]},
            })
            proc.feed({"type": "result", "subtype": "success", "usage": {}})

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    proc.feed({
        "type": "system", "subtype": "init",
        "session_id": "claude-1", "slash_commands": [],
    })
    return proc, ClaudeCodeCliDriver(lambda _args, _spec: proc, replay_timeout=1)


def _two_message_codex_driver():
    """A Codex turn with two agent messages that carry no ``itemId``."""
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {"server": "fake"}})
        elif method == "thread/start":
            proc.feed({"id": frame["id"], "result": {"thread": {"id": "th_1"}}})
        elif method == "turn/start":
            proc.feed({
                "id": frame["id"], "result": {"turn": {"id": "native-1"}},
            })
            proc.feed({
                "method": "turn/started", "params": {"turn": {"id": "native-1"}},
            })
            for text in ("first", "second"):
                proc.feed({
                    "method": "item/agentMessage/delta",
                    "params": {"delta": text},
                })
                proc.feed({
                    "method": "item/agentMessage/completed", "params": {},
                })
            proc.feed({
                "method": "turn/completed",
                "params": {"turn": {"status": "completed"}},
            })

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    return proc, CodexAppServerDriver(lambda _spec: proc)


async def _collect_until_turn_end(stream):
    collected = []
    async for value in stream:
        if value.turn_ref is not None:
            collected.append(value)
        kind = getattr(value.type, "value", value.type)
        if kind in {"turn.completed", "turn.abandoned"}:
            return collected
    raise AssertionError("event stream ended before the turn finished")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
async def test_two_output_blocks_in_one_turn_keep_distinct_block_ids(provider):
    """Reproduced bug: a frame-scoped block id aborts a two-block turn.

    Anthropic ``text`` blocks carry no ``id`` and Codex agent-message deltas may
    omit ``itemId``, so both drivers used to fall back to a value that is
    constant across the turn.  Two blocks then shared one public id, the
    projector suppressed the second ``start``, and the validator rejected the
    resulting ``end -> delta`` transition -- which the Runtime Manager turns
    into ``runtime_event_processing_failed`` and abandons the whole turn.
    """
    if provider == "claude-code":
        proc, driver = _two_block_claude_driver()
        await driver.open(RuntimeSpec("/workspace"))
    else:
        proc, driver = _two_message_codex_driver()
        await driver.open(RuntimeSpec("/workspace", model="gpt"))
    stream = driver.events()
    started = await driver.start_turn(
        TurnInput("hello", client_correlation_id="client-1")
    )
    assert started.turn_ref.value
    collected = await _collect_until_turn_end(stream)
    await driver.close()

    deltas = [
        value for value in collected
        if getattr(value.type, "value", value.type) == "turn.assistant_delta"
    ]
    block_ids = [value.data["block_id"] for value in deltas]
    assert [value.data["text"] for value in deltas] == ["first", "second"]
    assert len(set(block_ids)) == 2, block_ids

    # Replaying the driver's events must preserve lifecycle metadata while
    # keeping all assistant text local to the Runtime Manager.
    projector = RuntimeEventProjector(agent_id="agent_1", session_ref="s_1")
    validator = LifecycleValidator()
    projected_types = []
    for value in collected:
        for projected in projector.project_all(value):
            validator.accept(projected)
            projected_types.append(projected.type)
    assert "output.updated" not in projected_types
    assert validator.active_turn_ref is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
async def test_start_turn_write_failure_leaves_no_turn_or_request_pending(
    provider,
):
    """A provider write that fails must not strand per-turn bookkeeping.

    When the child dies between ``open()`` and the turn write, ``drain()``
    raises.  Claude used to write outside the ``try`` that clears its replay
    state, so ``_active`` stayed set and the next turn was refused as "one turn
    is already active"; Codex registered the pending future before the write,
    so a failed write leaked an entry that ``_fail_pending_requests`` later
    resolved with an exception no coroutine was left to retrieve.
    """
    if provider == "claude-code":
        proc, driver = _two_block_claude_driver()
        await driver.open(RuntimeSpec("/workspace"))
    else:
        proc, driver = _two_message_codex_driver()
        await driver.open(RuntimeSpec("/workspace", model="gpt"))

    working_drain = proc.stdin.drain

    async def broken_drain():
        raise ConnectionResetError("child died")

    proc.stdin.drain = broken_drain
    with pytest.raises(ConnectionResetError):
        await driver.start_turn(
            TurnInput("hello", client_correlation_id="client-1")
        )

    assert driver._active.value == ""
    assert driver._active_native_turn_id == ""
    if provider == "claude-code":
        assert driver._pending_replay is None
        assert driver._pending_content == ""
        assert driver._pending_uuid == ""
    else:
        assert driver._pending == {}

    # The failed turn never reached the provider, so the next one is admitted.
    proc.stdin.drain = working_drain
    retried = await driver.start_turn(
        TurnInput("hello", client_correlation_id="client-1")
    )
    assert retried.turn_ref.value
    await driver.close()


@pytest.mark.asyncio
async def test_codex_child_environment_merges_over_process_environment(
    monkeypatch,
):
    """``RuntimeSpec.environment`` is a delta, not the child's whole env.

    ``ClaudeCodeCliDriver`` merges it over ``os.environ``; Codex replaced the
    environment outright, so any spec carrying only overrides would launch
    ``codex app-server`` without PATH or HOME.
    """
    holder = {}

    def on_frame(frame):
        proc = holder["proc"]
        if frame.get("method") == "initialize":
            proc.feed({"id": frame["id"], "result": {"server": "fake"}})
        elif frame.get("method") == "thread/start":
            proc.feed({"id": frame["id"], "result": {"thread": {"id": "th_1"}}})

    proc = _FakeProcess(on_frame)
    holder["proc"] = proc
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("PUFFO_HARNESS_MARKER", "inherited")
    driver = CodexAppServerDriver()
    await driver.open(
        RuntimeSpec("/workspace", environment={"CODEX_HOME": "/tmp/codex"})
    )
    await driver.close()

    assert captured["env"]["CODEX_HOME"] == "/tmp/codex"
    assert captured["env"]["PUFFO_HARNESS_MARKER"] == "inherited"
    assert captured["env"]["PATH"] == os.environ["PATH"]


def _token_telemetry_driver(provider):
    holder: dict = {}

    def codex_on_frame(frame):
        proc = holder["proc"]
        method = frame.get("method")
        if method == "initialize":
            proc.feed({"id": frame["id"], "result": {}})
        elif method == "thread/start":
            proc.feed({"id": frame["id"], "result": {"thread": {"id": "th_token"}}})
        elif method == "turn/start":
            proc.feed({"id": frame["id"], "result": {"turn": {"id": "turn_token"}}})

    if provider in {"codex", "codex-absent"}:
        proc = _FakeProcess(codex_on_frame)
        holder["proc"] = proc
        expected = (140, 100, 110) if provider == "codex" else (140, 100, None)
        return proc, CodexAppServerDriver(lambda _spec: proc), expected

    def claude_on_frame(frame):
        if frame.get("type") == "control_request":
            holder["proc"].feed(
                _claude_context_response(frame, total_tokens=84)
            )
            return
        if frame.get("type") == "user":
            holder["proc"].feed({**frame, "isReplay": True})

    proc = _FakeProcess(claude_on_frame)
    proc.feed({
        "type": "system", "subtype": "init",
        "session_id": "claude-1", "slash_commands": [],
    })
    holder["proc"] = proc
    return (
        proc,
        ClaudeCodeCliDriver(lambda *_args: proc, replay_timeout=1),
        (40, 30, 84),
    )


def _feed_token_telemetry(provider, proc):
    if provider in {"codex", "codex-absent"}:
        base_last = {"inputTokens": 100, "cachedInputTokens": 60,
                     "outputTokens": 40}
        final_last = {"inputTokens": 150, "cachedInputTokens": 100,
                      "outputTokens": 60}
        if provider == "codex":
            base_last = {**base_last, "totalTokens": 80}
            final_last = {**final_last, "totalTokens": 110}
        proc.feed({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "last": base_last,
            "total": {"inputTokens": 500, "cachedInputTokens": 300,
                      "outputTokens": 80},
            "modelContextWindow": 258_400,
        }}})
        proc.feed({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "last": final_last,
            "total": {"inputTokens": 700, "cachedInputTokens": 400,
                      "outputTokens": 140},
            "modelContextWindow": 258_400,
        }}})
        proc.feed({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
    else:
        proc.feed({"type": "result", "subtype": "success", "usage": {
            "input_tokens": 25,
            "cache_creation_input_tokens": 15,
            "cache_read_input_tokens": 40,
            "output_tokens": 30,
        }})


def _reporter_emits(monkeypatch):
    emits = []

    async def fake_emit(agent_slug, event, payload):
        emits.append((event, payload))

    import puffo_agent.portal.control.reporter as reporter_mod

    monkeypatch.setattr(
        reporter_mod, "get_reporter", lambda: SimpleNamespace(emit=fake_emit)
    )
    return emits


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "codex-absent", "claude-code"])
async def test_terminal_token_telemetry_reaches_turn_complete_current_context(
    tmp_path, monkeypatch, provider,
):
    """Per-turn tokens must survive the Driver -> RuntimeManagerAdapter -> core
    handoff and surface as ``turn_complete.current_context``; post-2.0 dropped
    Codex ``last``/``total`` deltas, Claude cache semantics, and the whitelist.
    """
    proc, driver, expected = _token_telemetry_driver(provider)
    emits = _reporter_emits(monkeypatch)
    manager = RuntimeManager(
        driver, RuntimeSpec(str(tmp_path)), session_ref=SessionRef("logical-session")
    )
    adapter = RuntimeManagerAdapter(manager)
    await adapter.warm("system")
    agent = PuffoAgent(
        adapter, "system", str(tmp_path / "memory"),
        workspace_dir=str(tmp_path), agent_id="agent",
    )
    agent.log.append({"role": "user", "content": "hello"})
    task = asyncio.create_task(agent._run_turn_and_route("channel", "alice", None))
    await _wait_until(lambda: bool(manager._turn_refs))
    _feed_token_telemetry(provider, proc)
    await asyncio.wait_for(task, timeout=5)
    await _wait_until(lambda: len(emits) == 2)
    turn_complete = {event: payload for event, payload in emits}["turn_complete"]
    assert turn_complete["tokens"] == {
        "input": expected[0], "output": expected[1],
    }
    if expected[2] is None:
        assert "current_context" not in turn_complete
    else:
        assert turn_complete["current_context"] == expected[2]
    await adapter.aclose()


def _projection_event(type_, data, *, native=None):
    return HarnessEvent.normalized(
        type=type_, driver="codex",
        session_ref=SessionRef("native-session"),
        turn_ref=TurnRef("driver-turn"),
        native_session_id="native-session", native_turn_id="native-turn",
        data=data, native_payload=native,
    )


def _projection_events():
    return [
        _projection_event(
            "turn.started", {},
            native={"_puffo_internal": "frame", "secret": "NATIVE_FRAME_SECRET"},
        ),
        _projection_event("turn.assistant_delta",
                          {"text": "first half ", "block_id": "block-1"}),
        _projection_event("turn.assistant_delta",
                          {"text": "second half", "block_id": "block-1"}),
        _projection_event("turn.assistant_completed", {"block_id": "block-1"}),
        _projection_event("turn.assistant_completed", {"block_id": "block-1"}),
        _projection_event("turn.tool_started",
                          {"tool_call_ref": "tool-1",
                           "label": "mcp__puffo__read_inbox"},
                          native={"_puffo_internal": "tool_result",
                                  "arguments": {"secret_arg": "TOOL_ARG_SECRET"}}),
        _projection_event("turn.tool_started",
                          {"tool_call_ref": "tool-1",
                           "label": "mcp__puffo__read_inbox"}),
        _projection_event("turn.tool_completed",
                          {"tool_call_ref": "tool-1", "label": "read_inbox",
                           "outcome": "succeeded"},
                          native={"_puffo_internal": "tool_result",
                                  "result": "TOOL_RESULT_SECRET"}),
        _projection_event("turn.reasoning", {"text": "REASONING_SECRET"},
                          native={"reasoning": "COT_SECRET"}),
        _projection_event("turn.assistant_delta",
                          {"text": "[SILENT] do not surface",
                           "block_id": "block-2"}),
        _projection_event("turn.assistant_completed", {"block_id": "block-2"}),
        _projection_event("turn.assistant_delta",
                          {"text": "X" * 210 + "OVERBOUND_SECRET",
                           "block_id": "block-3"}),
        _projection_event("turn.assistant_completed", {"block_id": "block-3"}),
        _projection_event("turn.completed", {"outcome": "succeeded"}),
    ]


async def _feed_projection_events(driver, events):
    for event in events:
        await driver.queue.put(event)


def _build_projection_adapter(tmp_path, driver, monkeypatch):
    import puffo_agent.agent.harness.local_runtime as local_runtime
    from puffo_agent.agent.harness.local_runtime import (
        PreparedLocalRuntime,
        build_local_runtime_adapter,
    )

    class _StubPreparer:
        agent_id = "agent"

    prepared = PreparedLocalRuntime(
        harness_name="codex",
        spec=RuntimeSpec(str(tmp_path)),
        native_session_id="",
        migration_source="fresh",
        legacy_session_path=tmp_path / "legacy.json",
        preparer=_StubPreparer(),
    )
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db", max_rows=1)
    monkeypatch.setattr(local_runtime, "build_driver", lambda name: driver)
    adapter = build_local_runtime_adapter(
        prepared, outbox=outbox, logical_session_ref="logical-session"
    )
    return adapter, outbox


@pytest.mark.asyncio
async def test_local_sink_projects_only_bounded_safe_legacy_status(
    tmp_path, monkeypatch,
):
    """The local runtime sink must emit only bounded ``assistant_text`` (once per
    completed non-silent block) and label-only ``tool_use`` per normalized start,
    never duplicates, post-terminal fragments, or native/argument/reasoning
    material. A saturated outbox must not block this Profile Log or the turn.
    """
    from puffo_agent.agent.adapters.base import TurnContext

    driver = _ToolResultDriver()
    emits = _reporter_emits(monkeypatch)
    adapter, outbox = _build_projection_adapter(tmp_path, driver, monkeypatch)
    manager = adapter.manager
    ctx = TurnContext(
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        workspace_dir=str(tmp_path),
        claude_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
    )
    task = asyncio.create_task(adapter.run_turn(ctx))
    await _wait_until(lambda: bool(manager._turn_refs))
    await _feed_projection_events(driver, _projection_events())
    await asyncio.wait_for(task, timeout=5)
    await _feed_projection_events(driver, [
        _projection_event("turn.assistant_delta",
                          {"text": "STALE_TEXT", "block_id": "block-stale"}),
        _projection_event("turn.assistant_completed", {"block_id": "block-stale"}),
    ])
    await _wait_until(lambda: driver.queue.empty())
    await _wait_until(lambda: len(emits) == 3)
    await asyncio.sleep(0.02)

    assert emits == [
        ("assistant_text", {"text": "first half second half"}),
        ("tool_use", {"tool": "read_inbox"}),
        ("assistant_text", {"text": "X" * 200}),
    ]
    serialized = "".join(json.dumps(payload) for _, payload in emits)
    for sentinel in (
        "NATIVE_FRAME_SECRET", "TOOL_ARG_SECRET", "TOOL_RESULT_SECRET",
        "REASONING_SECRET", "COT_SECRET", "OVERBOUND_SECRET", "[SILENT]",
        "STALE_TEXT",
    ):
        assert sentinel not in serialized
    await adapter.aclose()
    outbox.close()
