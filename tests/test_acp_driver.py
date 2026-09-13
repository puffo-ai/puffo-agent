import asyncio
import os
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from acp import PROTOCOL_VERSION
from acp.connection import StreamDirection, StreamEvent
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    InitializeResponse,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)

from puffo_agent.agent.harness import build_driver
from puffo_agent.agent.harness.support.cleanup_errors import cleanup_errors
from puffo_agent.agent.harness.drivers.acp import (
    AcpDriver,
    AcpLaunchPlan,
    ValidatedLaunchPlan,
)
from puffo_agent.agent.harness.driver import (
    HarnessEventType,
    McpServerSpec,
    PermissionDecision,
    PermissionRef,
    RuntimeLifecycle,
    RuntimeSpec,
    SessionRef,
    TurnInput,
)
from puffo_agent.agent.harness.support.subprocess_io import ProcessTreeShutdownError
from puffo_agent.agent.harness.runtime.docker_runtime import (
    _sanitise_permission_mode as _docker_sanitise_permission_mode,
)
from puffo_agent.agent.harness.runtime.local_runtime import (
    VALID_PERMISSION_MODES,
    _sanitise_permission_mode as _local_sanitise_permission_mode,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = object()
        self.stdout = object()
        self.stderr = None
        self.returncode = None
        self._exit = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await self._exit

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        if not self._exit.done():
            self._exit.set_result(returncode)

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)


class _UncloseableProcess(_FakeProcess):
    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, client, observer, *, can_load=True) -> None:
        self.client = client
        self.observer = observer
        self.can_load = can_load
        self.prompt_result = asyncio.get_running_loop().create_future()
        self.calls = []
        self.closed = False

    async def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=self.can_load),
        )

    async def new_session(self, **kwargs):
        self.calls.append(("new_session", kwargs))
        return NewSessionResponse(session_id="acp_session")

    async def load_session(self, **kwargs):
        self.calls.append(("load_session", kwargs))
        return SimpleNamespace()

    async def prompt(self, **kwargs):
        self.calls.append(("prompt", kwargs))
        await self.observer(StreamEvent(
            StreamDirection.OUTGOING,
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt"},
        ))
        return await self.prompt_result

    async def cancel(self, **kwargs):
        self.calls.append(("cancel", kwargs))

    async def close(self):
        self.closed = True


class _Harness:
    def __init__(self, *, can_load=True) -> None:
        self.proc = _FakeProcess()
        self.conn = None
        self.client = None
        self.can_load = can_load

    def process_factory(self, command, spec):
        self.command = command
        self.spec = spec
        return self.proc

    def connection_factory(self, client, _stdin, _stdout, **kwargs):
        self.client = client
        self.conn = _FakeConnection(
            client,
            kwargs["observers"][0],
            can_load=self.can_load,
        )
        return self.conn


_PUFFO_CORE = McpServerSpec(
    name="puffo",
    command="/usr/bin/python3",
    args=("-m", "puffo_agent.mcp.puffo_core_server"),
    environment={"PUFFO_AGENT_ID": "agent_test"},
)


async def _collect_through(stream, type_):
    events = []
    async for event in stream:
        events.append(event)
        if event.type is type_:
            return events
    raise AssertionError(f"stream ended before {type_}")


@pytest.mark.asyncio
async def test_acp_open_negotiates_v1_and_loads_or_creates_session():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    opened = await driver.open(RuntimeSpec("/workspace", executable="agent"))

    assert opened.native_session_id == "acp_session"
    assert opened.capabilities.session_resume is True
    assert opened.capabilities.lifecycle is RuntimeLifecycle.PERSISTENT_CHILD
    assert opened.diagnostics.schema_source == (
        "agent-client-protocol==0.10.1/protocol-v1"
    )
    assert harness.command == ("agent",)
    await driver.close()

    resumed_harness = _Harness()
    resumed = AcpDriver(
        resumed_harness.process_factory,
        connection_factory=resumed_harness.connection_factory,
    )
    result = await resumed.open(
        RuntimeSpec("/workspace", executable="agent", launch_args=("acp",)),
        SessionRef("existing"),
    )
    assert result.resumed is True
    assert resumed_harness.conn.calls[-1][0] == "load_session"
    await resumed.close()


@pytest.mark.asyncio
async def test_prompt_admission_updates_and_response_form_one_terminal():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    stream = driver.events()
    started = await driver.start_turn(TurnInput("hello"))
    assert started.delivery == "jsonrpc_request_written"

    await harness.client.session_update(
        "acp_session",
        AgentMessageChunk(
            session_update="agent_message_chunk",
            message_id="message_1",
            content=TextContentBlock(type="text", text="answer"),
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallStart(
            session_update="tool_call",
            tool_call_id="tool_1",
            title="read_inbox",
            status="in_progress",
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            title="read_inbox",
            status="completed",
            raw_output="never public",
        ),
    )
    await harness.client.session_update(
        "acp_session",
        UsageUpdate(session_update="usage_update", used=10, size=100),
    )
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="end_turn"))

    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=1
    )
    assert [event.type for event in events].count(
        HarnessEventType.TURN_COMPLETED
    ) == 1
    delta = next(
        event for event in events
        if event.type is HarnessEventType.ASSISTANT_DELTA
    )
    assert delta.data == {"block_id": "message_1", "text": "answer"}
    tool = next(
        event for event in events
        if event.type is HarnessEventType.TOOL_COMPLETED
    )
    assert "never public" not in repr(tool.data)
    await driver.close()


@pytest.mark.asyncio
async def test_post_commit_admission_extension_is_private_and_normalized():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))

    await harness.client.session_update(
        "acp_session",
        ToolCallStart(
            session_update="tool_call",
            tool_call_id="tool_1",
            title="mcp__puffo__read_inbox",
            status="in_progress",
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            status="completed",
        ),
    )
    binding = "a" * 64
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            field_meta={
                "puffo.admission/1": {
                    "toolCallId": "tool_1",
                    "binding": binding,
                },
            },
        ),
    )
    # A duplicate fact is ignored, so one commit cannot fire twice.
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            field_meta={
                "puffo.admission/1": {
                    "toolCallId": "tool_1",
                    "binding": binding,
                },
            },
        ),
    )
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="end_turn"))

    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=1
    )
    admissions = [
        event for event in events
        if isinstance(event.native_diagnostic, dict)
        and event.native_diagnostic.get("provider_context_committed") is True
    ]
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission.data == {
        "tool_call_ref": "tool_1",
        "label": "mcp__puffo__read_inbox",
        "outcome": "succeeded",
    }
    assert binding not in repr(admission.data)
    assert admission.native_diagnostic == {
        "_puffo_internal": "tool_result",
        "provider_context_committed": True,
        "tool_call_id": "tool_1",
        "tool_name": "mcp__puffo__read_inbox",
        "admission_binding": binding,
        "is_error": False,
    }
    await driver.close()


@pytest.mark.asyncio
async def test_post_commit_admission_requires_prior_success_and_matching_id():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))
    extension = {
        "puffo.admission/1": {
            "toolCallId": "tool_1",
            "binding": "b" * 64,
        },
    }

    # Metadata before a successful terminal cannot witness a future commit.
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            field_meta=extension,
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallStart(
            session_update="tool_call",
            tool_call_id="tool_1",
            title="read_inbox",
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            status="failed",
        ),
    )
    await harness.client.session_update(
        "acp_session",
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id="tool_1",
            field_meta=extension,
        ),
    )
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="end_turn"))
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.TURN_COMPLETED), timeout=1
    )
    assert not any(
        isinstance(event.native_diagnostic, dict)
        and event.native_diagnostic.get("provider_context_committed") is True
        for event in events
    )
    await driver.close()


@pytest.mark.asyncio
async def test_permission_request_waits_for_typed_driver_resolution():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    # Explicitly non-bypass: RuntimeSpec defaults to "bypassPermissions", so
    # without this the driver answers on its own and this test would silently
    # stop covering the human path it exists to pin.
    await driver.open(
        RuntimeSpec("/workspace", executable="agent", permission_mode="ask")
    )
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))
    permission = asyncio.create_task(harness.client.request_permission(
        [
            PermissionOption(
                option_id="deny",
                name="Deny",
                kind="reject_once",
            ),
            PermissionOption(
                option_id="allow",
                name="Allow once",
                kind="allow_once",
            ),
        ],
        "acp_session",
        ToolCallStart(
            session_update="tool_call",
            tool_call_id="tool_1",
            title="shell",
        ),
    ))
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.PERMISSION_REQUESTED),
        timeout=1,
    )
    request = events[-1]
    ref = PermissionRef(str(request.data["permission_ref"]))
    receipt = await driver.resolve_permission(
        ref, PermissionDecision.APPROVE
    )
    assert receipt.accepted is True
    response = await asyncio.wait_for(permission, timeout=1)
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "allow"
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="cancelled"))
    await driver.close()


@pytest.mark.asyncio
async def test_unknown_extension_is_explicitly_unsupported_and_observable():
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    stream = driver.events()
    with pytest.raises(RequestError) as exc_info:
        await harness.client.ext_method("vendor/steer", {})
    assert exc_info.value.code == -32601
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.RUNTIME_WARNING), timeout=1
    )
    assert events[-1].data == {
        "code": "unsupported_extension",
        "method": "_vendor/steer",
    }
    await driver.close()


def test_acp_factory_is_closed_and_explicit():
    assert isinstance(build_driver("acp"), AcpDriver)


@pytest.mark.asyncio
async def test_launch_validator_sees_complete_immutable_plan_at_spawn_boundary():
    events = []
    harness = _Harness()

    def validate(plan):
        events.append(("validate", plan))
        assert plan.executable == "agent"
        assert plan.argv == ("agent", "acp", "--agent-dir", "/agent")
        assert plan.environment["ACP_TOKEN"] == "secret"
        assert plan.cwd == "/workspace"
        (server,) = plan.mcp_servers
        assert server.name == "puffo"
        assert server.command == "/usr/bin/python3"
        assert server.args == ("-m", "puffo_agent.mcp.puffo_core_server")
        assert dict(server.environment) == {"PUFFO_AGENT_ID": "agent_test"}
        # Sealed means sealed for the nested carriers too: what the
        # validator saw is byte-for-byte what the wire call will convert.
        assert isinstance(server.args, tuple)
        with pytest.raises(TypeError):
            server.environment["PUFFO_AGENT_ID"] = "changed"
        with pytest.raises(TypeError):
            plan.environment["ACP_TOKEN"] = "changed"

    def process_factory(command, plan):
        events.append(("spawn", plan))
        assert command == plan.argv
        return harness.proc

    driver = AcpDriver(
        process_factory,
        connection_factory=harness.connection_factory,
        launch_validator=validate,
    )
    await driver.open(RuntimeSpec(
        "/workspace",
        executable="agent",
        launch_args=("acp", "--agent-dir", "/agent"),
        environment={"ACP_TOKEN": "secret"},
        # A spec that actually carries a server: with the default empty
        # tuple the plan assertion above passes whatever the Driver does.
        mcp_servers=(_PUFFO_CORE,),
    ))

    assert [name for name, _ in events] == ["validate", "spawn"]
    assert events[0][1] is events[1][1]
    await driver.close()


@pytest.mark.asyncio
async def test_validated_launch_plan_cannot_be_minted_or_spawn_bypassed():
    raw = AcpLaunchPlan(
        executable="agent",
        argv=("agent",),
        environment={},
        cwd="/workspace",
        mcp_servers=(),
    )
    with pytest.raises(TypeError, match="only be created by AcpDriver"):
        ValidatedLaunchPlan(raw, _token=object())

    driver = AcpDriver()
    with pytest.raises(TypeError, match="requires a ValidatedLaunchPlan"):
        await driver._spawn(RuntimeSpec("/workspace", executable="agent"))


@pytest.mark.asyncio
async def test_process_factory_type_error_is_not_retried():
    calls = 0

    def failing_factory(_command, _plan):
        nonlocal calls
        calls += 1
        raise TypeError("factory failed after starting")

    driver = AcpDriver(failing_factory)
    with pytest.raises(TypeError, match="factory failed after starting"):
        await driver.open(RuntimeSpec("/workspace", executable="agent"))
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
async def test_close_reaps_acp_descendant_and_closes_transports(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.acp._SHUTDOWN_GRACE_SECONDS",
        0.2,
    )
    pid_path = tmp_path / "acp-descendant.pid"
    child_script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    parent_script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        f"{child_script!r}], stdout=sys.stdout, stderr=sys.stderr); "
        f"open({str(pid_path)!r}, 'w').write(str(child.pid)); "
        "time.sleep(60)"
    )
    spawned = {}

    async def process_factory(_command, _plan):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned["proc"] = proc
        return proc

    harness = _Harness()
    driver = AcpDriver(
        process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    await _wait_for_path(pid_path)

    await asyncio.wait_for(driver.close(), timeout=2)

    descendant_pid = int(pid_path.read_text())
    _assert_process_exited(descendant_pid)
    proc = spawned["proc"]
    assert proc._transport.is_closing()
    assert proc.stdin.transport.is_closing()
    assert proc.stdout._transport.is_closing()
    assert proc.stderr._transport.is_closing()


@pytest.mark.asyncio
async def test_close_reports_acp_transport_abandonment(monkeypatch):
    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.acp._SHUTDOWN_GRACE_SECONDS",
        0.01,
    )
    harness = _Harness()
    harness.proc = _UncloseableProcess()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))

    with pytest.raises(ProcessTreeShutdownError):
        await driver.close()


@pytest.mark.asyncio
async def test_close_finishes_tail_cleanup_after_unexpected_shutdown_error(
    monkeypatch,
):
    async def fail_shutdown(*_args, **_kwargs):
        raise RuntimeError("unexpected shutdown failure")

    monkeypatch.setattr(
        "puffo_agent.agent.harness.drivers.acp.shutdown_process_tree",
        fail_shutdown,
    )
    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    events = driver.events()

    with pytest.raises(RuntimeError, match="unexpected shutdown failure"):
        await driver.close()

    assert driver._prompt_task is None
    assert driver._watcher is None
    assert driver._stderr_reader is None
    assert [event async for event in events]


@pytest.mark.asyncio
async def test_cancelled_close_finishes_process_shutdown(monkeypatch):
    from puffo_agent.agent.harness.drivers import acp as acp_driver

    monkeypatch.setattr(acp_driver, "_SHUTDOWN_GRACE_SECONDS", 0.01)
    entered = asyncio.Event()
    original_shutdown = acp_driver.shutdown_process_tree

    async def observed_shutdown(*args, **kwargs):
        entered.set()
        await original_shutdown(*args, **kwargs)

    monkeypatch.setattr(acp_driver, "shutdown_process_tree", observed_shutdown)

    class SlowKillProcess(_FakeProcess):
        def terminate(self):
            pass

        def kill(self):
            asyncio.get_running_loop().call_later(0.001, self.exit, -9)

    harness = _Harness()
    harness.proc = SlowKillProcess()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(RuntimeSpec("/workspace", executable="agent"))
    task = asyncio.create_task(driver.close())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    assert cleanup_errors(exc_info.value) == ()
    assert harness.proc.returncode == -9
    assert driver._proc is None
    assert driver._watcher is None


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


async def _wait_for_path(path) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"child did not create {path}")


@pytest.mark.parametrize(
    "launch_args",
    [
        pytest.param(
            ("acp", "--runtime-id", "rt_x", "--profile", "puffo-v0"),
            id="puffo-v0",
        ),
        pytest.param(
            ("acp", "--runtime-id", "rt_x", "--profile", "puffo-v1"),
            id="puffo-v1",
        ),
        pytest.param(("acp", "--agent-dir", "/agent"), id="generic-acp"),
    ],
)
def test_spec_mcp_servers_are_forwarded_into_the_acp_launch_plan(
    launch_args,
):
    """The Driver is transport, not policy: whatever the runtime projected
    into ``spec.mcp_servers`` is converted to the ACP wire shape and sealed
    into the plan, for the constrained and the generic profile alike.
    Keeping puffo-v0 empty is the runtime projection's job — pinned in
    ``test_puffo_v0_projection_keeps_mcp_servers_empty``."""
    seen = []
    driver = AcpDriver(
        launch_validator=lambda plan: seen.append(plan.mcp_servers)
    )
    spec = RuntimeSpec(
        "/workspace",
        executable="lingtai-agent",
        launch_args=launch_args,
        mcp_servers=(_PUFFO_CORE,),
    )

    # Inspect the final pre-spawn seam directly. Reaching into this local
    # object is intentional: this test guards plan construction and must not
    # start a real provider when ``lingtai-agent`` happens to be installed.
    driver._validate_launch_plan(spec)

    assert len(seen) == 1
    (server,) = seen[0]
    assert server.name == "puffo"
    assert server.command == "/usr/bin/python3"
    assert server.args == ("-m", "puffo_agent.mcp.puffo_core_server")
    assert dict(server.environment) == {"PUFFO_AGENT_ID": "agent_test"}
    assert isinstance(server.args, tuple)
    with pytest.raises(TypeError):
        server.environment["INJECTED"] = "value"


@pytest.mark.parametrize(
    "resume, expected_call",
    [
        pytest.param(None, "new_session", id="session-new"),
        pytest.param(SessionRef("existing"), "load_session", id="session-load"),
    ],
)
@pytest.mark.asyncio
async def test_session_calls_receive_the_projected_mcp_servers(
    resume, expected_call
):
    """The wire value, not just the plan: both ``session/new`` and
    ``session/load`` carry the converted server list — resuming an agent
    must not silently drop its tools."""
    harness = _Harness(can_load=True)
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(
        RuntimeSpec(
            "/workspace",
            executable="agent",
            launch_args=("acp", "--agent-dir", "/agent"),
            mcp_servers=(_PUFFO_CORE,),
        ),
        resume,
    )

    sent = dict(harness.conn.calls)[expected_call]["mcp_servers"]
    assert isinstance(sent, list)
    (server,) = sent
    assert isinstance(server, McpServerStdio)
    assert server.name == "puffo"
    assert server.command == "/usr/bin/python3"
    assert server.args == ["-m", "puffo_agent.mcp.puffo_core_server"]
    assert [(e.name, e.value) for e in server.env] == [
        ("PUFFO_AGENT_ID", "agent_test")
    ]
    await driver.close()


@pytest.mark.asyncio
async def test_bypass_permissions_answers_without_a_human():
    """``bypassPermissions`` must not park a tool call on an operator.

    The runtime only ever supplies this mode (VALID_PERMISSION_MODES holds
    exactly one value), so an unattended agent depends on this path entirely.
    """

    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(
        RuntimeSpec(
            "/workspace", executable="agent", permission_mode="bypassPermissions"
        )
    )
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))
    # No resolve_permission() anywhere: if the driver waits, this await times out.
    response = await asyncio.wait_for(
        harness.client.request_permission(
            [
                PermissionOption(
                    option_id="deny", name="Deny", kind="reject_once"
                ),
                PermissionOption(
                    option_id="allow", name="Allow once", kind="allow_once"
                ),
            ],
            "acp_session",
            ToolCallStart(
                session_update="tool_call", tool_call_id="tool_1", title="shell"
            ),
        ),
        timeout=1,
    )
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "allow"
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.PERMISSION_REQUESTED), timeout=1
    )
    # The record must say it was auto-allowed, not look like an unanswered prompt.
    assert events[-1].data["auto_allowed"] is True
    # Nothing retained: no pending future, so no cross-request grant exists to
    # reuse. This is the property that keeps the peer's turn-scoped permission
    # contract intact.
    assert driver._permissions == {}
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="cancelled"))
    await driver.close()


@pytest.mark.asyncio
async def test_bypass_permissions_will_not_allow_with_a_reject_only_option_set():
    """Auto-approval needs something that actually grants.

    ``_preferred_permission`` falls back to ``options[0]``; reusing it here
    would return outcome="selected" carrying a *reject* option id -- an
    "approval" that denies. With nothing allow-shaped on offer the driver must
    fall back to asking a human.
    """

    harness = _Harness()
    driver = AcpDriver(
        harness.process_factory,
        connection_factory=harness.connection_factory,
    )
    await driver.open(
        RuntimeSpec(
            "/workspace", executable="agent", permission_mode="bypassPermissions"
        )
    )
    stream = driver.events()
    await driver.start_turn(TurnInput("hello"))
    pending = asyncio.create_task(
        harness.client.request_permission(
            [
                PermissionOption(
                    option_id="deny", name="Deny", kind="reject_once"
                ),
            ],
            "acp_session",
            ToolCallStart(
                session_update="tool_call", tool_call_id="tool_1", title="shell"
            ),
        )
    )
    events = await asyncio.wait_for(
        _collect_through(stream, HarnessEventType.PERMISSION_REQUESTED), timeout=1
    )
    assert events[-1].data["auto_allowed"] is False
    # Still pending on a human rather than silently "allowing" with a denial.
    assert not pending.done()
    ref = PermissionRef(str(events[-1].data["permission_ref"]))
    await driver.resolve_permission(ref, PermissionDecision.DENY)
    response = await asyncio.wait_for(pending, timeout=1)
    assert response.outcome.outcome == "cancelled"
    harness.conn.prompt_result.set_result(PromptResponse(stop_reason="cancelled"))
    await driver.close()


def test_widening_permission_modes_needs_an_operator_answer_path():
    """Tripwire: nothing in production can currently ask a human.

    Both runtimes coerce every configured ``permission_mode`` to
    ``bypassPermissions``, and every RuntimeSpec is built from that sanitised
    value — so the waiting branch of ``_request_permission`` is reachable only
    from tests. The docstring above
    (``test_bypass_permissions_answers_without_a_human``) states that as prose;
    this binds it.

    Widening this set is a one-line change that silently activates the wait.
    Before doing so, confirm something presents ``turn.permission_requested``
    to an operator and calls ``runtime.resolve_permission``. Puffo sets no
    timeout of its own, so without that path every tool call runs out the ACP
    peer's timeout and is then DENIED.
    """

    assert VALID_PERMISSION_MODES == frozenset({"bypassPermissions"}), (
        "permission_mode gained a reachable value, so the ask-a-human branch "
        "is now live. Verify an operator-facing answer path exists "
        "(turn.permission_requested -> runtime.resolve_permission) before "
        "shipping this, or every tool call becomes a timeout-then-deny."
    )


def test_both_runtimes_agree_on_which_permission_modes_exist():
    """The docker sanitiser hardcodes its own set instead of importing one.

    Two copies of the same guard drift silently: widening the local constant
    would leave docker still coercing, so the same agent config would ask a
    human on one runtime and not the other. Compare behaviour, not the
    literals, so this keeps working if either side is refactored.
    """

    probes = ["bypassPermissions", "ask", "acceptEdits", "plan", "", "default"]
    local = {mode: _local_sanitise_permission_mode(mode, "agent") for mode in probes}
    docker = {mode: _docker_sanitise_permission_mode(mode, "agent") for mode in probes}
    assert local == docker, (
        "local_runtime and docker_runtime disagree about permission modes; "
        "they hold separate copies of the same allow-list."
    )
