from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer
from mcp.server.fastmcp import FastMCP

from puffo_agent.agent.adapters.base import TurnContext
from puffo_agent.agent.global_inbox_runtime import (
    ActiveBoundaryAdapter,
    BaselineAdapter,
    GlobalInboxRuntime,
)
from puffo_agent.agent.harness.driver import (
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
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    RuntimeManagerAdapter,
    RuntimeStateError,
)
from puffo_agent.agent.send_coordinator import (
    KEYLESS_CHANNEL_SEND_PATH,
    SendCoordinator,
)
from puffo_agent.agent.shared_content import HELD_SEND_RECONSIDERATION_GUIDANCE
from puffo_agent.agent.message_store import (
    MessageStore,
    ProcessingState,
    ReceiptDisposition,
)
from puffo_agent.mcp._host_mcp import PuffoRpcClient
from puffo_agent.mcp.puffo_core_tools import (
    PuffoCoreToolsConfig,
    register_core_tools,
)
from puffo_agent.portal import rpc_service
from puffo_agent.portal.host_mcp_handler import HostMcpContext
from puffo_agent.portal.local_service_auth import issue_local_service_token


class ContractDriver(Driver):
    """Deterministic provider event source; all admission remains real."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[HarnessEvent] = asyncio.Queue()
        self.turn = TurnRef("driver-turn")

    async def open(self, spec, resume=None):
        return RuntimeOpened(
            RuntimeRef("runtime"),
            SessionRef("native-session"),
            "native-session",
            False,
            SimpleNamespace(),
            SimpleNamespace(),
        )

    async def start_turn(self, input: TurnInput):
        return TurnStarted(self.turn, "native-turn")

    async def steer_turn(self, turn, input):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn):
        return UnsupportedCapability("cancel")

    async def context_status(self):
        return UnsupportedCapability("context_status")

    async def compact(self, request: CompactRequest):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request: PermissionRef, decision: PermissionDecision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                yield await self.queue.get()

        return iterate()

    async def close(self):
        return None


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)


def _tool_text(result) -> str:
    blocks = result[0] if isinstance(result, tuple) else result
    if not isinstance(blocks, list):
        return str(blocks)
    return "".join(
        getattr(block, "text", str(block))
        for block in blocks
    )


async def _seed(store: MessageStore, *, envelope_id: str, channel_id: str, seq: int, text: str):
    await store.store_receipt(
        {
            "envelope_id": envelope_id,
            "envelope_kind": "channel",
            "sender_slug": "peer-0001",
            "space_id": "sp_1",
            "channel_id": channel_id,
            "content": text,
            "content_type": "text/plain",
            "sent_at": seq,
            "is_encrypted": True,
        },
        server_seq=seq,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="contract test",
        received_at=seq,
    )


class HeldTransport:
    """Keyless transport that exposes one held send, then one commit."""

    keyless = True

    def __init__(self, *, held_envelope_id: str, held_seq: int) -> None:
        self.held_envelope_id = held_envelope_id
        self.held_seq = held_seq
        self.calls: list[tuple[str, dict]] = []

    async def post_unsigned(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        assert path == KEYLESS_CHANNEL_SEND_PATH
        freshness = body["freshness"]
        if len(self.calls) == 1:
            return {
                "state": "held",
                "envelope_id": "held-send-attempt",
                "context_baseline_seq": freshness["context_baseline_seq"],
                "seen_seq": freshness["seen_seq"],
                "latest_seq": self.held_seq,
                "latest_envelope_id": self.held_envelope_id,
            }
        request_baseline = freshness["context_baseline_seq"]
        return {
            "state": "sent",
            "envelope_id": "send-anyway-result",
            "seq": freshness["seen_seq"] + 1,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": freshness["mode"],
                "context_baseline_seq": (
                    request_baseline
                    if request_baseline is not None
                    else freshness["seen_seq"]
                ),
                "seen_seq": freshness["seen_seq"],
                "latest_seq_before_send": freshness["seen_seq"],
            },
        }


class BoundaryHarness:
    def __init__(
        self,
        *,
        store: MessageStore,
        driver: ContractDriver,
        manager: RuntimeManager,
        adapter: RuntimeManagerAdapter,
        runtime: GlobalInboxRuntime,
        rpc: PuffoRpcClient,
        mcp: FastMCP,
        turn_task: asyncio.Task,
        staged_responses: list[dict],
        transport: HeldTransport,
    ):
        self.store = store
        self.driver = driver
        self.manager = manager
        self.adapter = adapter
        self.runtime = runtime
        self.rpc = rpc
        self.mcp = mcp
        self.turn_task = turn_task
        self.staged_responses = staged_responses
        self.transport = transport

    @property
    def session_id(self) -> str:
        return "native-session"

    @property
    def native_turn_id(self) -> str:
        return "native-turn"

    @property
    def active_turn_id(self) -> str:
        return self.runtime.active.turn_id

    async def active_boundary(self, channel_id: str) -> int | None:
        return await ActiveBoundaryAdapter(
            self.store, self.runtime.active
        ).get_active_turn_through_seq("sp_1", channel_id)

    async def emit_tool_result(
        self,
        *,
        tool_name: str,
        arguments: dict,
        result,
        native_session_id: str = "native-session",
        native_turn_id: str = "native-turn",
        is_error: bool = False,
    ) -> None:
        await self.driver.queue.put(HarnessEvent.normalized(
            type="turn.tool_completed",
            driver="contract",
            session_ref=SessionRef(native_session_id),
            turn_ref=self.driver.turn,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            data={
                "tool_call_ref": "contract-tool-call",
                "label": tool_name,
                "outcome": "failed" if is_error else "succeeded",
            },
            native_payload={
                "_puffo_internal": "tool_result",
                "tool_call_id": "contract-tool-call",
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                "is_error": is_error,
            },
        ))

    async def finish(self, outcome: str = "succeeded") -> None:
        await self.driver.queue.put(HarnessEvent(
            type="turn.completed",
            driver="contract",
            session_ref=SessionRef("native-session"),
            turn_ref=self.driver.turn,
            native_session_id="native-session",
            native_turn_id="native-turn",
            data={"outcome": outcome},
        ))
        if outcome == "succeeded":
            await self.turn_task
        else:
            with pytest.raises(RuntimeStateError):
                await self.turn_task


async def _seed_boundary_store(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    for envelope_id, channel_id, seq, text in (
        ("history-peer", "ch-history", 2, "history peer body"),
        ("inbox-peer", "ch-inbox", 3, "inbox peer body"),
        ("held-earlier-peer", "ch-held", 5, "earlier held peer body"),
        ("held-peer", "ch-held", 7, "held peer body"),
    ):
        await _seed(
            store, envelope_id=envelope_id, channel_id=channel_id,
            seq=seq, text=text,
        )
    await store.store_receipt(
        {
            "envelope_id": "held-self",
            "envelope_kind": "channel",
            "sender_slug": "agent-contract",
            "space_id": "sp_1",
            "channel_id": "ch-held",
            "content": "previous self contribution",
            "content_type": "text/plain",
            "sent_at": 6,
            "is_encrypted": True,
        },
        server_seq=6,
        disposition=ReceiptDisposition.TERMINAL,
        reason="self echo",
        received_at=6,
    )
    return store


async def _start_boundary_runtime(tmp_path, store):
    driver = ContractDriver()
    manager = RuntimeManager(driver, RuntimeSpec(str(tmp_path)))
    adapter = RuntimeManagerAdapter(manager)
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    turn_task = asyncio.create_task(adapter.run_turn(TurnContext(
        system_prompt="contract",
        messages=[{"role": "user", "content": "active turn"}],
    )))
    await _wait_until(lambda: manager.active_turn_ref is not None)
    runtime.active.turn_id = str(manager.active_turn_ref)
    runtime.active.provider_session_id = adapter.get_provider_session_id()
    runtime.active.provider_turn_id = manager.native_turn_id
    return driver, manager, adapter, runtime, turn_task


def _boundary_coordinator(store, runtime):
    transport = HeldTransport(held_envelope_id="held-peer", held_seq=7)
    coordinator = SendCoordinator(
        slug="agent-contract", keystore=None, http_client=transport,
        data_client=store, baseline_source=BaselineAdapter(store),
        active_turn_source=ActiveBoundaryAdapter(store, runtime.active),
        held_recovery_source=runtime.held_recovery_source,
        provider_session_id=runtime.active.provider_session_id,
    )
    runtime.coordinator = coordinator
    return transport, coordinator


async def _start_boundary_rpc(tmp_path, store, runtime, coordinator):
    ctx = HostMcpContext(
        agent_id="agent-contract", slug="agent-contract",
        operator_slug="operator", host_home=tmp_path, agent_home=tmp_path,
        harness="claude-code", keystore=None, http_client=None,
        message_client=SimpleNamespace(global_runtime=runtime),
        send_coordinator=coordinator,
    )
    rpc_service.set_rpc_resolver(
        lambda agent_id: ctx if agent_id == "agent-contract" else None
    )
    server = TestServer(rpc_service.build_app(
        rpc_service.RpcServiceConfig(enabled=True, port=0)
    ))
    await server.start_server()
    rpc = PuffoRpcClient(
        str(server.make_url("/")),
        "agent-contract",
        issue_local_service_token("agent-contract"),
    )
    staged_responses: list[dict] = []

    # This harness deliberately wires both backends. Record whichever one the
    # tools actually resolve — in-process tools prefer the live runtime, the
    # subprocess/RPC site has only the client — so the contract assertions
    # stay about *staging*, not about which transport served it.
    def _record(owner, name="stage_model_visible_read"):
        real = getattr(owner, name)

        async def record_stage(**kwargs):
            result = await real(**kwargs)
            staged_responses.append(result)
            return result

        setattr(owner, name, record_stage)

    _record(rpc)
    _record(runtime)
    return server, rpc, staged_responses


def _boundary_mcp(store, runtime, rpc, coordinator):
    cfg = PuffoCoreToolsConfig(
        slug="agent-contract", device_id="device-contract", keystore=None,
        http_client=SimpleNamespace(keyless=False), data_client=store,
        agent_id="agent-contract", rpc_client=rpc,
        send_coordinator=coordinator, inbox_runtime=runtime,
    )
    mcp = FastMCP("contract")
    register_core_tools(mcp, cfg)
    return mcp


@pytest_asyncio.fixture
async def boundary_harness(tmp_path: Path):
    store = await _seed_boundary_store(tmp_path)
    driver, manager, adapter, runtime, turn_task = await _start_boundary_runtime(
        tmp_path, store,
    )
    transport, coordinator = _boundary_coordinator(store, runtime)
    server, rpc, staged_responses = await _start_boundary_rpc(
        tmp_path, store, runtime, coordinator,
    )
    mcp = _boundary_mcp(store, runtime, rpc, coordinator)

    harness = BoundaryHarness(
        store=store,
        driver=driver,
        manager=manager,
        adapter=adapter,
        runtime=runtime,
        rpc=rpc,
        mcp=mcp,
        turn_task=turn_task,
        staged_responses=staged_responses,
        transport=transport,
    )
    try:
        yield harness
    finally:
        if not turn_task.done():
            turn_task.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)
        await rpc.close()
        await server.close()
        rpc_service.set_rpc_resolver(None)
        await manager.close()
        await store.close()


@pytest.mark.asyncio
async def test_model_visible_history_and_inbox_join_the_same_active_turn(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness

    history_arguments = {
        "target": "channel:sp_1:ch-history",
        "limit": 50,
    }
    history_call = await h.mcp.call_tool(
        "read_history", history_arguments
    )
    assert not isinstance(history_call, tuple)
    history_text = _tool_text(history_call)
    assert "history peer body" in history_text
    assert re.search(r"\[puffo:model-visible-read:[^]]+\]", history_text)
    assert h.staged_responses[-1]["state"] == "staged"

    history_row = await h.store.get_message_by_envelope("history-peer")
    assert history_row is not None
    assert history_row.processing_state is ProcessingState.PENDING
    assert h.runtime.active.visible_message_ids == []
    assert await h.active_boundary("ch-history") is None

    history_receipt = re.search(
        r"(\[puffo:model-visible-read:[^]]+\])", history_text
    ).group(1)
    await h.emit_tool_result(
        tool_name="read_history",
        arguments=history_arguments,
        result=history_text,
    )
    await _wait_until(
        lambda: h.runtime.active.through_by_channel.get(
            ("sp_1", "ch-history")
        ) == 2
    )
    assert history_receipt in history_text
    assert await h.active_boundary("ch-history") == 2
    history_row = await h.store.get_message_by_envelope("history-peer")
    assert history_row is not None
    assert history_row.processing_state is ProcessingState.IN_TURN
    assert h.runtime.active.message_ids == ["history-peer"]
    assert h.runtime.active.visible_message_ids == ["history-peer"]

    inbox_call = await h.mcp.call_tool(
        "read_inbox",
        {"target": "channel:sp_1:ch-inbox", "limit": 1},
    )
    assert not isinstance(inbox_call, tuple)
    inbox_text = _tool_text(inbox_call)
    assert "inbox peer body" in inbox_text
    assert "admission_receipt" not in inbox_text
    inbox_row = await h.store.get_message_by_envelope("inbox-peer")
    assert inbox_row is not None
    assert inbox_row.processing_state is ProcessingState.IN_TURN
    assert await h.active_boundary("ch-inbox") == 3
    assert h.runtime.active.message_ids == ["history-peer", "inbox-peer"]
    assert h.runtime.active.visible_message_ids == ["history-peer", "inbox-peer"]

    await h.finish()


@pytest.mark.asyncio
async def test_exact_held_chain_admits_only_at_original_send_result_boundary(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness
    target = "channel:sp_1:ch-held"
    send_arguments = {"channel": "ch-held", "text": "held draft"}

    held_call = await h.mcp.call_tool("send_message", send_arguments)
    assert not isinstance(held_call, tuple)
    held_result = _tool_text(held_call)
    assert '[send_result context_version=1 state="held"' in held_result
    assert "synchronized=true" in held_result
    assert "latest_seq=7" in held_result
    assert 'latest_message_id="held-peer"' in held_result
    assert '[window context_version=1 kind="held_basis"' in held_result
    assert '[window context_version=1 kind="held_new_context"' in held_result
    assert held_result.count("previous self contribution") == 1
    assert "held peer body" in held_result
    assert HELD_SEND_RECONSIDERATION_GUIDANCE in held_result
    assert re.search(r"\[puffo:model-visible-read:[^]]+\]", held_result)
    assert len(h.transport.calls) == 1

    row = await h.store.get_message_by_envelope("held-peer")
    assert row is not None
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-held") is None

    # The exact original result, including the continuation receipt, admits
    # the displayed pending row exactly once.
    await h.emit_tool_result(
        tool_name="send_message",
        arguments=send_arguments,
        result=held_result,
    )
    await asyncio.sleep(0)
    row = await h.store.get_message_by_envelope("held-peer")
    assert row.processing_state is ProcessingState.PENDING
    await _wait_until(
        lambda: h.runtime.active.through_by_channel.get(
            ("sp_1", "ch-held")
        ) == 7
    )
    row = await h.store.get_message_by_envelope("held-peer")
    assert row.processing_state is ProcessingState.IN_TURN
    self_echo = await h.store.get_message_by_envelope("held-self")
    assert self_echo is not None
    assert self_echo.processing_state is None
    assert await h.active_boundary("ch-held") == 7
    assert h.runtime.active.message_ids == ["held-earlier-peer", "held-peer"]
    assert h.runtime.active.visible_message_ids == [
        "held-earlier-peer",
        "held-self",
        "held-peer",
    ]

    # A duplicate callback cannot change the turn or reoffer the row.
    await h.emit_tool_result(
        tool_name="send_message", arguments=send_arguments, result=held_result,
    )
    assert h.runtime.active.visible_message_ids == [
        "held-earlier-peer",
        "held-self",
        "held-peer",
    ]

    inbox_call = await h.mcp.call_tool(
        "read_inbox", {"target": target, "limit": 1}
    )
    assert "[pending_messages context_version=1 message_count=0]" in _tool_text(
        inbox_call
    )

    sent_call = await h.mcp.call_tool(
        "send_message",
        {**send_arguments, "send_anyway": True},
    )
    assert not isinstance(sent_call, tuple)
    sent = _tool_text(sent_call)
    assert '[send_result context_version=1 state="sent"' in sent
    assert len(h.transport.calls) == 2
    assert h.transport.calls[-1][1]["freshness"] == {
        "context_baseline_seq": None,
        "seen_seq": 7,
        "mode": "send_anyway",
    }

    await h.finish()


@pytest.mark.asyncio
async def test_held_admission_freezes_provider_turn_at_staging(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness
    arguments = {"channel": "ch-held", "text": "held draft"}
    held_result = _tool_text(
        await h.mcp.call_tool("send_message", arguments)
    )
    assert '[send_result context_version=1 state="held"' in held_result

    # The event and active runtime can agree on a later provider turn, but
    # neither may reinterpret a continuation staged for the original turn.
    h.runtime.active.provider_turn_id = "replacement-provider-turn"
    await h.emit_tool_result(
        tool_name="send_message",
        arguments=arguments,
        result=held_result,
        native_turn_id="replacement-provider-turn",
    )
    await asyncio.sleep(0)
    row = await h.store.get_message_by_envelope("held-peer")
    assert row is not None
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-held") is None
    assert h.runtime.active.visible_message_ids == []
    await h.finish()


@pytest.mark.asyncio
async def test_rpc_response_without_provider_completion_keeps_pending_and_unseen(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness
    history_call = await h.mcp.call_tool(
        "read_history",
        {
            "target": "channel:sp_1:ch-history",
            "limit": 50,
        },
    )
    history_text = _tool_text(history_call)
    assert "history peer body" in history_text
    assert h.staged_responses[-1]["state"] == "staged"

    row = await h.store.get_message_by_envelope("history-peer")
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-history") is None
    await h.finish("failed")
    row = await h.store.get_message_by_envelope("history-peer")
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-history") is None


@pytest.mark.asyncio
async def test_real_provider_correlation_rejects_empty_failed_and_mismatched_results(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness
    arguments = {
        "target": "channel:sp_1:ch-history",
        "limit": 50,
    }
    history_call = await h.mcp.call_tool(
        "read_history", arguments
    )
    history_text = _tool_text(history_call)
    receipt = re.search(
        r"(\[puffo:model-visible-read:[^]]+\])", history_text
    ).group(1)
    wrong_receipt = "[puffo:model-visible-read:wrong-receipt-marker]"
    cases = [
        ("get_thread_history", arguments, history_text, "native-session", "native-turn", False),
        ("read_history", {**arguments, "target": "channel:sp_1:wrong"}, history_text, "native-session", "native-turn", False),
        (
            "read_history",
            arguments,
            f"history peer body\n{wrong_receipt}",
            "native-session",
            "native-turn",
            False,
        ),
        ("read_history", arguments, history_text, "native-session", "wrong-turn", False),
        ("read_history", arguments, history_text, "wrong-session", "native-turn", False),
        ("read_history", arguments, history_text, "native-session", "native-turn", True),
    ]
    for tool_name, tool_args, result, session_id, turn_id, is_error in cases:
        await h.emit_tool_result(
            tool_name=tool_name,
            arguments=tool_args,
            result=result,
            native_session_id=session_id,
            native_turn_id=turn_id,
            is_error=is_error,
        )
        await asyncio.sleep(0)
        assert await h.active_boundary("ch-history") is None
        assert h.runtime.active.visible_message_ids == []
        row = await h.store.get_message_by_envelope("history-peer")
        assert row.processing_state is ProcessingState.PENDING
        blocked_call = await h.mcp.call_tool(
            "send_message",
            {
                "channel": "ch-history",
                "text": "negative-case override",
                "send_anyway": True,
            },
        )
        blocked = _tool_text(blocked_call)
        assert 'state="failed"' in blocked
        assert 'error_kind="reconsideration_ineligible"' in blocked
        assert not h.transport.calls

    await h.emit_tool_result(
        tool_name="read_history",
        arguments=arguments,
        result=history_text.replace(receipt, receipt),
    )
    await _wait_until(
        lambda: h.runtime.active.through_by_channel.get(
            ("sp_1", "ch-history")
        ) == 2
    )
    assert await h.active_boundary("ch-history") == 2
    assert h.runtime.active.visible_message_ids == ["history-peer"]
    await h.finish()


@pytest.mark.asyncio
async def test_empty_or_failed_rpc_read_has_no_admission_receipt_or_lifecycle_change(
    boundary_harness: BoundaryHarness,
):
    h = boundary_harness
    empty = await h.mcp.call_tool(
        "read_inbox",
        {"target": "channel:sp_1:does-not-exist", "limit": 1},
    )
    empty_text = _tool_text(empty)
    assert "[pending_messages context_version=1 message_count=0]" in empty_text
    assert "admission_receipt" not in empty_text
    assert await h.active_boundary("ch-inbox") is None

    await h.emit_tool_result(
        tool_name="send_message",
        arguments={"channel": "ch-inbox", "text": "content-free"},
        result={"state": "sent"},
    )
    await asyncio.sleep(0)
    row = await h.store.get_message_by_envelope("inbox-peer")
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-inbox") is None

    with pytest.raises(RuntimeError):
        await h.rpc.stage_model_visible_read(
            space_id="sp_1",
            channel_id="ch-inbox",
            through_seq=99,
            through_envelope_id="missing",
            tool_name="get_channel_history",
            tool_arguments={"channel": "ch-inbox"},
        )
    row = await h.store.get_message_by_envelope("inbox-peer")
    assert row.processing_state is ProcessingState.PENDING
    assert await h.active_boundary("ch-inbox") is None
    await h.finish()
