"""One table-driven regression test for the worker Global Inbox status lifecycle.

The durable Global Inbox turn — not WebSocket receipt — owns status reporting:
provider admission emits one provisional busy heartbeat, while ``read_inbox``
owns ``StatusReporter.begin_turn`` and the first real processing row. Terminal
cleanup settles the exact active union, and synthetic ``intro-prompt-*``
envelopes show busy state without creating Server processing rows.
"""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.errors import ProviderFailureError
from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.status_reporter import StatusReporter
from puffo_agent.portal.worker_run import GlobalInboxStatusLifecycle

from test_global_inbox_runtime import _ContinuationAdapter, make_store, receipt
from test_status_reporter import FakeHttp

INTRO_ID = "intro-prompt-ch_x-1778641626040"


class _LifecycleRunner:
    """Drive one durable Global Inbox turn through notice + read_inbox."""

    def __init__(self, adapter, *, expand, outcome, error):
        self.adapter = adapter
        self.expand = expand
        self.outcome = outcome
        self.error = error
        self.runtime = None

    async def __call__(self, planned):
        await self.adapter.admit()
        if self.outcome == "no_read":
            return None
        await self.runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        if self.expand:
            await self.runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        if self.outcome == "failure":
            raise self.error
        if self.outcome == "cancelled":
            raise AgentAPIError("rate limit", is_auth=False)
        if self.outcome in {"retry", "retry_exhausted"}:
            raise AgentAPIError("rate limit", is_auth=False)
        return None

    async def handle_global_inbox_retry(self, _planned):
        if self.outcome == "retry_exhausted":
            raise AgentAPIError("rate limit", is_auth=False)
        return None


_CASES = [
    {"id": "success_expanded", "outcome": "success", "expand": True},
    {"id": "success_then_second_turn", "outcome": "success", "expand": False, "second": True},
    {"id": "provider_failure", "outcome": "failure", "expand": False},
    {"id": "quota_drained", "outcome": "failure", "expand": False,
     "error_code": "quota_exhausted"},
    {"id": "cancelled", "outcome": "cancelled", "expand": False},
    {"id": "retry_same_turn", "outcome": "retry", "expand": False},
    {"id": "retry_exhausted", "outcome": "retry_exhausted", "expand": False},
    {"id": "success_without_read", "outcome": "no_read", "expand": False},
    {"id": "synthetic_intro", "outcome": "success", "expand": False, "synthetic": True},
]


def _begin_runs(http) -> dict[str, str]:
    """Map each admitted message id to the run id its processing/start minted."""
    return {
        path.split("/")[2]: body["run_id"]
        for path, body in http.calls
        if path.endswith("/processing/start")
    }


def _batch_runs(http) -> list[list[dict]]:
    return [
        body["runs"]
        for path, body in http.calls
        if path == "/messages/processing/end:batch"
    ]


def _assert_status_wire(case: dict, http, lifecycle) -> None:
    begin_runs = _begin_runs(http)
    batch_runs = _batch_runs(http)
    turns = 2 if case.get("second") else 1
    # Clean per-turn state: a subsequent case/turn cannot inherit runs.
    assert lifecycle._notice_began is False
    assert lifecycle._began is False
    heartbeats = [
        body for path, body in http.calls if path == "/agents/me/heartbeat"
    ]
    if case.get("synthetic"):
        # Busy is shown for the synthetic turn, but no processing row reaches
        # the wire for the synthetic id.
        assert [body["status"] for body in heartbeats] == ["busy", "idle"]
        assert "current_message_id" not in heartbeats[0]
        assert begin_runs == {}
        assert batch_runs == []
        assert not any("/processing/" in path for path, _ in http.calls)
        return
    if case["outcome"] == "no_read":
        assert [body["status"] for body in heartbeats] == ["busy", "idle"]
        assert heartbeats[0]["current_message_id"] == "m1"
        assert begin_runs == {}
        assert batch_runs == []
        return
    assert len(heartbeats) == turns
    assert [body["status"] for body in heartbeats] == ["busy"] * turns
    assert heartbeats[0]["current_message_id"] == "m1"
    if case.get("second"):
        assert heartbeats[1]["current_message_id"] == "m3"
    # Exactly one begin per logical turn, and one terminal batch per turn.
    assert len(begin_runs) == turns
    assert len(batch_runs) == turns
    assert len(set(begin_runs.values())) == turns
    run_ids = list(begin_runs.values())
    if case.get("second"):
        assert batch_runs[0] == [
            {"run_id": run_ids[0], "message_id": "m1", "succeeded": True}
        ]
        assert batch_runs[1] == [
            {"run_id": run_ids[1], "message_id": "m3", "succeeded": True}
        ]
        return
    if case["outcome"] in ("success", "retry"):
        if case["expand"]:
            # Mid-turn active-union expansion settles the exact union in one
            # batch: first entry reuses the begin run id, later entries are new.
            assert batch_runs[0][0] == {
                "run_id": run_ids[0], "message_id": "m1", "succeeded": True,
            }
            assert batch_runs[0][1]["message_id"] == "m2"
            assert batch_runs[0][1]["succeeded"] is True
            assert batch_runs[0][1]["run_id"] != run_ids[0]
        else:
            assert batch_runs[0] == [
                {"run_id": run_ids[0], "message_id": "m1", "succeeded": True}
            ]
        return
    entry = batch_runs[0][0]
    assert entry["run_id"] == run_ids[0]
    assert entry["message_id"] == "m1"
    assert entry["succeeded"] is False
    assert entry["error_text"] and len(entry["error_text"]) <= 1024
    if case["outcome"] == "cancelled":
        assert "cancelled" in entry["error_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
async def test_global_inbox_turn_owns_one_status_lifecycle(tmp_path, monkeypatch, case):
    monkeypatch.setattr(
        "puffo_agent.portal.control.store.current_machine_id", lambda: None,
    )
    store = await make_store(tmp_path)
    http = FakeHttp()
    process_outcomes = []
    lifecycle = GlobalInboxStatusLifecycle(
        StatusReporter(http, heartbeat_interval_s=999)
    )
    if case.get("synthetic"):
        await store.store_local_event(
            {
                "envelope_id": INTRO_ID,
                "envelope_kind": "channel",
                "sender_slug": "system",
                "channel_id": "ch-x",
                "space_id": "sp-x",
                "content": "introduce yourself",
                "sent_at": 1,
            },
            reason="intro",
        )
    else:
        await receipt(store, "m1", 1)
        if case["expand"]:
            await receipt(store, "m2", 2)

    adapter = _ContinuationAdapter()
    runner = _LifecycleRunner(
        adapter,
        expand=case["expand"],
        outcome=case["outcome"],
        error=ProviderFailureError(
            "The selected provider model is unavailable.",
            error_code=case.get("error_code", "provider_unavailable"),
        ),
    )
    async def retry_sleep(_delay):
        if case["outcome"] == "cancelled":
            raise asyncio.CancelledError()
        await asyncio.sleep(0)

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
        status_lifecycle=lifecycle,
        process_outcome=lambda outcome, error: process_outcomes.append(
            (outcome, error)
        ),
        max_api_retries=1,
        retry_sleep=retry_sleep,
    )
    runner.runtime = runtime

    if case["outcome"] == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await runtime.process_once()
    else:
        assert await runtime.process_once() is True
    if case.get("second"):
        await receipt(store, "m3", 3)
        assert await runtime.process_once() is True
    _assert_status_wire(case, http, lifecycle)
    expected_outcome = {
        "failure": "provider_failed",
        "cancelled": "cancelled",
        "retry_exhausted": "api_error_abandoned",
    }.get(case["outcome"], "succeeded")
    if case.get("error_code") == "quota_exhausted":
        # spent quota splits out of provider_failed: hold, don't retry
        expected_outcome = "drained"
    assert process_outcomes[0][0] == expected_outcome
    await store.close()


@pytest.mark.asyncio
async def test_crash_recovery_reuses_and_settles_original_processing_run(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "m1", 1)
    http = FakeHttp()

    original_adapter = _ContinuationAdapter()
    original_lifecycle = GlobalInboxStatusLifecycle(
        StatusReporter(http, heartbeat_interval_s=999)
    )
    original = GlobalInboxRuntime(
        store=store,
        adapter=original_adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        status_lifecycle=original_lifecycle,
    )
    planned = await original.plan_pending(items=tuple(await store.get_pending()))
    assert planned is not None
    await original._start_local_turn(planned)
    original_adapter.register_admission_callback(
        lambda event: original._admit(planned, event),
        planned.planning_cycle_key,
    )
    await original_adapter.admit()

    class RecoveredRunner:
        async def handle_global_inbox_retry(self, _planned):
            return None

    process_outcomes = []
    recovered = GlobalInboxRuntime(
        store=store,
        adapter=_ContinuationAdapter(),
        run_turn=RecoveredRunner(),
        workspace=tmp_path,
        status_lifecycle=GlobalInboxStatusLifecycle(
            StatusReporter(http, heartbeat_interval_s=999)
        ),
        process_outcome=lambda outcome, error: process_outcomes.append(
            (outcome, error)
        ),
    )

    assert await recovered.recover_current_turn() is True
    assert process_outcomes == [("succeeded", None)]
    starts = [
        body
        for path, body in http.calls
        if path.endswith("/processing/start")
    ]
    assert len(starts) == 2
    assert starts[0]["run_id"] == starts[1]["run_id"]
    assert _batch_runs(http) == [[{
        "run_id": starts[0]["run_id"],
        "message_id": "m1",
        "succeeded": True,
    }]]
    await store.close()
