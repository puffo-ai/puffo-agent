from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from puffo_agent.agent.adapters.base import Adapter, TurnContext, TurnResult
from puffo_agent.agent.context_controller import (
    AdmissionCandidate,
    CompactionResult,
    ContextCapabilities,
    ContextController,
    ContextDecision,
    ContextSnapshot,
    DecisionOutcome,
    FALLBACK_CONTEXT_WINDOW,
    ProviderAdmissionEvent,
    RolloverResult,
    normalize_context_snapshot,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def snapshot(used=60_000, window=200_000, source="provider"):
    return ContextSnapshot(used, window, source, NOW)


def candidate(
    batch=30_000, wrapper=5_000, reserve=5_000, minimum=0, cycle="cycle-1",
):
    return AdmissionCandidate(
        planning_cycle_key=cycle,
        formatted_batch_tokens=batch,
        wrapper_overhead_tokens=wrapper,
        output_tool_reserve_tokens=reserve,
        minimum_formatted_batch_tokens=minimum,
        payload={"opaque": True},
    )


class FakeProvider:
    def __init__(
        self,
        snapshots,
        *,
        compact_results=(),
        native_compaction=False,
        rollover=False,
    ):
        self.snapshots = list(snapshots)
        self.compact_results = list(compact_results)
        self.capabilities = ContextCapabilities(
            native_compaction=native_compaction,
            rollover=rollover,
            native_measurement=True,
        )
        self.calls = []

    async def get_context_snapshot(self):
        self.calls.append("snapshot")
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def get_context_capabilities(self):
        return self.capabilities

    async def compact_context(self):
        self.calls.append("compact")
        return self.compact_results.pop(0)

    async def rollover_context(self):
        self.calls.append("rollover")
        return RolloverResult(True, "old", None)

    def get_provider_session_id(self):
        return "provider-session"


def test_projection_soft_target_exact_boundary_and_one_token_pressure():
    provider = FakeProvider([snapshot()])
    controller = ContextController(provider)
    admitted = asyncio.run(controller.decide(candidate()))
    assert admitted.outcome is DecisionOutcome.ADMIT
    assert admitted.projected_tokens == 100_000

    pressured = asyncio.run(
        ContextController(FakeProvider([snapshot()])).decide(candidate(batch=30_001))
    )
    assert pressured.outcome is not DecisionOutcome.ADMIT
    assert ContextController.projection(snapshot(), candidate(batch=30_001)) == 100_001


def test_projection_includes_all_four_terms():
    base = snapshot(used=11)
    cand = candidate(batch=13, wrapper=17, reserve=19)
    assert ContextController.projection(base, cand) == 60


def test_fallback_precedence_and_inspectable_source():
    provider = normalize_context_snapshot(
        used_tokens=1,
        provider_context_window=123,
        verified_model_context_window=456,
        measured_at=NOW,
    )
    model = normalize_context_snapshot(
        used_tokens=1,
        verified_model_context_window=456,
        measured_at=NOW,
    )
    fallback = normalize_context_snapshot(used_tokens=1, measured_at=NOW)
    assert (provider.context_window, provider.source) == (123, "provider")
    assert (model.context_window, model.source) == (456, "verified_model")
    assert fallback.context_window == FALLBACK_CONTEXT_WINDOW == 200_000
    assert "fallback" in fallback.source


def test_contract_records_are_frozen_and_decisions_are_closed_set():
    records = [
        snapshot(),
        ContextCapabilities(),
        CompactionResult(False),
        RolloverResult(False),
        candidate(),
        ContextDecision(DecisionOutcome.ADMIT, candidate(), snapshot(), 1),
        ProviderAdmissionEvent("k", None, NOW),
    ]
    for record in records:
        with pytest.raises(FrozenInstanceError):
            setattr(record, next(iter(record.__dataclass_fields__)), "changed")
    assert {outcome.value for outcome in DecisionOutcome} == {
        "admit", "replan", "shrink", "rollover", "degraded",
    }


def test_replan_order_and_arrival_after_compaction():
    provider = FakeProvider(
        [snapshot(100_000), snapshot(10_000)],
        compact_results=[CompactionResult(True)],
        native_compaction=True,
    )

    async def replan(old):
        provider.calls.append("replan")
        return AdmissionCandidate(
            "cycle-2", 7, 5, 5, payload={"arrival": "new-message"},
        )

    decision = asyncio.run(ContextController(provider, replan).decide(candidate()))
    assert decision.outcome is DecisionOutcome.REPLAN
    assert decision.candidate.payload == {"arrival": "new-message"}
    assert provider.calls == ["snapshot", "compact", "snapshot", "replan"]


def test_successful_compaction_is_bounded_per_lifecycle_cycle():
    provider = FakeProvider(
        [snapshot(100_000), snapshot(100_000)],
        compact_results=[CompactionResult(True)],
        native_compaction=True,
    )
    controller = ContextController(provider, lambda old: asyncio.sleep(0, result=old))
    assert asyncio.run(controller.decide(candidate())).outcome is DecisionOutcome.REPLAN
    assert asyncio.run(controller.decide(candidate())).outcome in {
        DecisionOutcome.SHRINK, DecisionOutcome.DEGRADED,
    }
    assert provider.calls.count("compact") == 1


def test_failed_compaction_bounded_to_two_then_shrink():
    provider = FakeProvider(
        [snapshot(60_000)],
        compact_results=[CompactionResult(False), CompactionResult(False)],
        native_compaction=True,
    )
    controller = ContextController(provider)
    for _ in range(3):
        decision = asyncio.run(controller.decide(candidate(batch=30_001, minimum=1)))
    assert provider.calls.count("compact") == 2
    assert decision.outcome is DecisionOutcome.SHRINK


def test_failed_compaction_bound_is_independent_per_planning_cycle():
    provider = FakeProvider(
        [snapshot(60_000)],
        compact_results=[CompactionResult(False) for _ in range(4)],
        native_compaction=True,
    )
    controller = ContextController(provider)
    for cycle in ("cycle-a", "cycle-b"):
        for _ in range(3):
            decision = asyncio.run(
                controller.decide(
                    candidate(batch=30_001, minimum=1, cycle=cycle),
                ),
            )
        assert decision.outcome is DecisionOutcome.SHRINK
    assert provider.calls.count("compact") == 4


def test_rollover_is_bounded_when_fixed_occupancy_cannot_fit():
    provider = FakeProvider([snapshot(100_000), snapshot(0)], rollover=True)
    controller = ContextController(provider)
    first = asyncio.run(controller.decide(candidate(minimum=30_000)))
    second = asyncio.run(controller.decide(candidate(minimum=30_000)))
    assert first.outcome is DecisionOutcome.ROLLOVER
    assert second.outcome is DecisionOutcome.ADMIT
    assert provider.calls.count("rollover") == 1


def test_degraded_when_control_unsupported_and_nothing_can_fit():
    result = asyncio.run(
        ContextController(FakeProvider([snapshot(100_000)])).decide(
            candidate(minimum=30_000),
        )
    )
    assert result.outcome is DecisionOutcome.DEGRADED


class MinimalAdapter(Adapter):
    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        return TurnResult("ok")


def test_default_adapter_compatibility_and_one_shot_admission_callback():
    adapter = MinimalAdapter()
    events = []

    async def callback(event):
        events.append(event)
        raise RuntimeError("consumer error")

    adapter.register_admission_callback(callback, "cycle")
    event = ProviderAdmissionEvent("cycle", None, NOW)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter._fire_admission_callback(event))
    asyncio.run(adapter._fire_admission_callback(event))
    assert events == [event]
    assert adapter.get_provider_session_id() is None
    snap = asyncio.run(adapter.get_context_snapshot())
    assert snap.context_window == 200_000
    assert "estimated" in snap.source


def test_provider_protocol_is_fakeable_without_adapter():
    provider = FakeProvider([snapshot()])
    result = asyncio.run(ContextController(provider).decide(candidate()))
    assert result.outcome is DecisionOutcome.ADMIT
