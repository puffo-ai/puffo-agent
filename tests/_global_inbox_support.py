"""Prior-context delivery helpers for the global inbox runtime test suites."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from puffo_agent.agent.context_controller import ProviderAdmissionEvent
from puffo_agent.agent.global_inbox_runtime import (
    GlobalInboxRuntime,
    TrackingSendDelegate,
)
from puffo_agent.agent.message_store import (
    PRIOR_CONTEXT_MAX_BYTES,
    PRIOR_CONTEXT_MAX_ITEMS,
    ProcessingState,
    ReceiptDisposition,
)

from test_global_inbox_runtime import (
    Adapter,
    make_store,
    projection_metadata,
    receipt,
)


class _PriorProviderRecorder:
    def __init__(self):
        self.turn_inputs = []

    def capture(self, read_inbox_result):
        self.turn_inputs.append(read_inbox_result)


class _PriorProviderAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.continuation = None
        self.continuation_key = ""
        self.continuation_calls = []

    def register_continuation_callback(self, callback, planning_cycle_key, **_kwargs):
        self.continuation = callback
        self.continuation_key = planning_cycle_key

    async def admit_continuation(self, provider_turn_id):
        callback, self.continuation = self.continuation, None
        assert callback is not None
        self.continuation_calls.append(self.continuation_key)
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=self.continuation_key,
            provider_session_id=self.session, provider_turn_id=provider_turn_id,
            tool_call_id=f"read-{provider_turn_id}",
            admitted_at=datetime.now(timezone.utc),
        ))


class _PriorServerTransport:
    def __init__(self):
        self.calls = []

    async def send(self, request=None, **kwargs):
        self.calls.append(dict(request or kwargs))
        number = len(self.calls)
        return {
            "state": "sent",
            "envelope_id": "agent-contribution" if number == 1 else "agent-followup",
            "seq": 2 if number == 1 else 5,
        }


class _PriorCoordinator:
    provider_session_id = None

    def __init__(self, transport):
        self.http_client = SimpleNamespace(keyless=False)
        self.transport = transport

    async def send(self, request=None, **kwargs):
        return await self.transport.send(request, **kwargs)


def _prior_runtime(tmp_path, store, bodies):
    adapter = _PriorProviderAdapter()
    transport = _PriorServerTransport()
    coordinator = _PriorCoordinator(transport)
    provider = _PriorProviderRecorder()
    provider_inputs = []
    holder = {}

    async def run(planned):
        runtime = holder["runtime"]
        turn_number = len(provider_inputs) + 1
        await adapter.admit(provider_turn_id=f"provider-turn-{turn_number}")
        page = await runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        assert len(page["messages"]) == 1
        assert set(page) == {
            "context_version", "messages", "prior_context",
            "prior_context_has_more", "next_cursor", "has_more",
            "remaining_count", "snapshot_generation", "correlation_receipt",
        }
        await adapter.admit_continuation(provider_turn_id=f"provider-read-{turn_number}")
        provider_inputs.append({
            "fresh_notice": planned.provider_input, "read_inbox_result": page,
        })
        if turn_number == 1:
            assert bodies.human in page["messages"][0]
            assert page["prior_context"] == []
            assert page["prior_context_has_more"] is False
            provider.capture(page)
            sent = await runtime.send_delegate.send({
                "destination": "ch-1", "text": bodies.contribution,
                "visibility_level": "human",
            })
            assert sent["state"] == "sent"
            await receipt(
                store, "agent-contribution", 2, sender="agent",
                disposition=ReceiptDisposition.TERMINAL,
                content=bodies.contribution,
            )
            return
        decision_input = json.dumps(page, sort_keys=True)
        assert bodies.peer in page["messages"][0]
        assert bodies.human in decision_input
        assert bodies.contribution in decision_input
        self_metadata = next(
            projection_metadata(block) for block in page["prior_context"]
            if projection_metadata(block)["envelope_id"] == "agent-contribution"
        )
        assert self_metadata["sender_slug"] == "agent"
        assert self_metadata["is_self"] is True
        provider.capture(page)

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
        coordinator=coordinator, agent_id="current-agent-id",
        send_mode_keys=("current-agent-id", "agent"),
    )
    runtime.send_delegate = TrackingSendDelegate(coordinator, runtime.attempts, runtime)
    holder["runtime"] = runtime
    return runtime, adapter, transport, provider, provider_inputs


async def _prior_lifecycle(store):
    result = {}
    for envelope_id in ("human-origin", "agent-contribution"):
        row = await store.get_message_by_envelope(envelope_id)
        assert row is not None
        result[envelope_id] = (
            row.processing_state, row.receipt_disposition, row.model_visible_at,
        )
    return result


async def _assert_first_prior_turn(store, runtime):
    assert await runtime.process_once()
    first = await store.get_message_by_envelope("human-origin")
    contribution = await store.get_message_by_envelope("agent-contribution")
    assert first is not None and first.processing_state is ProcessingState.PROCESSED
    assert contribution is not None
    assert contribution.processing_state is None
    assert contribution.receipt_disposition is ReceiptDisposition.TERMINAL


async def _assert_second_prior_turn(store, runtime, adapter, provider_inputs, before):
    assert await runtime.process_once()
    peer = await store.get_message_by_envelope("peer-progress")
    future = await store.get_message_by_envelope("future-pending")
    assert peer is not None and peer.processing_state is ProcessingState.PROCESSED
    assert future is not None and future.processing_state is ProcessingState.PENDING
    assert len(adapter.continuation_calls) == 2
    second = provider_inputs[1]["read_inbox_result"]
    prior_blocks = second["prior_context"]
    messages = [projection_metadata(block) for block in second["messages"]]
    prior = [projection_metadata(block) for block in prior_blocks]
    assert [item["envelope_id"] for item in messages] == ["peer-progress"]
    assert [item["envelope_id"] for item in prior] == [
        "human-origin", "agent-contribution",
    ]
    assert messages[0]["is_self"] is False
    assert prior[0]["is_self"] is False
    self_metadata = next(
        item for item in prior if item["envelope_id"] == "agent-contribution"
    )
    after = await _prior_lifecycle(store)
    assert after == before
    assert len(prior_blocks) <= PRIOR_CONTEXT_MAX_ITEMS
    assert sum(len(block.encode("utf-8")) for block in prior_blocks) <= PRIOR_CONTEXT_MAX_BYTES
    return peer, future, prior, self_metadata, after


async def _run_prior_context_delivery_case(tmp_path):
    """Run two fresh provider Turns against only durable/tool-return input."""
    store = await make_store(tmp_path)
    bodies = SimpleNamespace(
        human="Please outline the current task.",
        contribution="I mapped the current dependency.",
        peer="A peer supplied additional context for the same interaction.",
    )
    await receipt(store, "human-origin", 1, sender="human", content=bodies.human)
    runtime, adapter, transport, provider, inputs = _prior_runtime(
        tmp_path, store, bodies,
    )
    await _assert_first_prior_turn(store, runtime)
    await receipt(store, "peer-progress", 3, sender="peer-agent", content=bodies.peer)
    await receipt(
        store, "future-pending", 4, sender="peer-agent",
        content="future pending work must not leak",
    )
    before = await _prior_lifecycle(store)
    peer, future, prior, self_metadata, lifecycle = await _assert_second_prior_turn(
        store, runtime, adapter, inputs, before,
    )
    result = {
        "provider_turns": len(inputs), "provider_inputs": inputs,
        "transport_calls": len(transport.calls), "peer_state": peer.processing_state,
        "future_state": future.processing_state,
        "prior_ids": [item["envelope_id"] for item in prior],
        "human_body": bodies.human, "first_contribution": bodies.contribution,
        "peer_body": bodies.peer, "self_metadata": self_metadata,
        "prior_lifecycle": lifecycle, "provider_turn_inputs": provider.turn_inputs,
    }
    await store.close()
    return result
