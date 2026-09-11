"""Finalize-time cover reconciliation and one-shot renotice."""

import pytest

from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.message_store_models import ProcessingState

from test_global_inbox_runtime import Adapter, make_store, receipt


def _human_content(text: str) -> dict:
    return {"text": text, "sender_type": "human"}


class _ReadingRunner:
    """Admit the turn, read the whole Inbox, optionally declare covers."""

    def __init__(self, adapter, covers=()):
        self.adapter = adapter
        self.covers = tuple(covers)
        self.runtime = None
        self.provider_inputs = []

    async def __call__(self, planned):
        self.provider_inputs.append(planned.provider_input)
        await self.adapter.admit()
        await self.runtime.read_inbox(limit=50, tool_arguments={"limit": 50})
        if self.covers:
            await self.runtime.store.add_message_covers(
                self.covers, source="send", by_envelope_id="reply-1",
            )


def _build_runtime(store, tmp_path, *, covers=()):
    adapter = Adapter()
    runner = _ReadingRunner(adapter, covers=covers)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
    )
    runner.runtime = runtime
    runtime.test_runner = runner
    return runtime


@pytest.mark.asyncio
async def test_uncovered_human_message_is_observed_not_renoticed_by_default(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "h1", 1, content=_human_content("question"))
    runtime = _build_runtime(store, tmp_path)
    assert not runtime.covers_renotice_enabled
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PROCESSED
    assert not row.renotified
    await store.close()


@pytest.mark.asyncio
async def test_uncovered_human_message_is_renoticed_exactly_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "h1", 1, content=_human_content("question"))
    runtime = _build_runtime(store, tmp_path)
    assert runtime.covers_renotice_enabled

    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PENDING
    assert row.renotified

    # The redelivered row goes uncovered again; the one-shot bit means the
    # second completed turn is terminal.
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PROCESSED

    # The first notice carried no uncovered section; the redelivery
    # notice announced the backlog in metadata before any read.
    first, second = runtime.test_runner.provider_inputs
    assert "[uncovered" not in first
    assert "[uncovered context_version=1 message_count=1]" in second
    await store.close()


@pytest.mark.asyncio
async def test_covered_and_agent_messages_complete_without_renotice(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "human-covered", 1, content=_human_content("ping"))
    await receipt(store, "agent-note", 2, content={
        "text": "fyi", "sender_type": "agent",
    })
    runtime = _build_runtime(store, tmp_path, covers=("human-covered",))
    assert await runtime.process_once()
    for envelope_id in ("human-covered", "agent-note"):
        row = await store.get_message_by_envelope(envelope_id)
        assert row.processing_state is ProcessingState.PROCESSED
        assert not row.renotified
    await store.close()


@pytest.mark.asyncio
async def test_reconciliation_failure_still_redelivers_once(
    tmp_path, monkeypatch,
):
    """The safety net's own failure must not silently settle the turn."""
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "h1", 1, content=_human_content("question"))
    runtime = _build_runtime(store, tmp_path)

    async def boom(_turn_id):
        raise RuntimeError("reconciliation read failed")

    original = runtime._reconcile_uncovered_messages
    runtime._reconcile_uncovered_messages = boom
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PENDING
    assert row.renotified

    # Second delivery reconciles normally; the one-shot bit makes it final.
    runtime._reconcile_uncovered_messages = original
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PROCESSED
    await store.close()


@pytest.mark.asyncio
async def test_reconciliation_failure_without_renotice_completes_plainly(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "h1", 1, content=_human_content("question"))
    runtime = _build_runtime(store, tmp_path)
    assert not runtime.covers_renotice_enabled

    async def boom(_turn_id):
        raise RuntimeError("reconciliation read failed")

    runtime._reconcile_uncovered_messages = boom
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PROCESSED
    assert not row.renotified
    await store.close()


@pytest.mark.asyncio
async def test_reconciliation_and_fallback_failure_completes_plainly(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "h1", 1, content=_human_content("question"))
    runtime = _build_runtime(store, tmp_path)

    async def boom(_turn_id):
        raise RuntimeError("reconciliation read failed")

    async def boom_fallback(**_kwargs):
        raise RuntimeError("fallback write failed")

    runtime._reconcile_uncovered_messages = boom
    monkeypatch.setattr(
        store, "complete_turn_renotice_unrenotified", boom_fallback,
    )
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("h1")
    assert row.processing_state is ProcessingState.PROCESSED
    await store.close()


@pytest.mark.asyncio
async def test_partial_coverage_renotices_only_the_uncovered_rows(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "answered", 1, content=_human_content("thread A"))
    await receipt(store, "forgotten", 2, content=_human_content("thread B"))
    runtime = _build_runtime(store, tmp_path, covers=("answered",))
    assert await runtime.process_once()
    answered = await store.get_message_by_envelope("answered")
    assert answered.processing_state is ProcessingState.PROCESSED
    forgotten = await store.get_message_by_envelope("forgotten")
    assert forgotten.processing_state is ProcessingState.PENDING
    assert forgotten.renotified
    await store.close()


class _ScriptedRunner:
    """Run a scripted sequence of per-turn behaviors."""

    def __init__(self, adapter, behaviors):
        self.adapter = adapter
        self.behaviors = list(behaviors)
        self.runtime = None
        self.reads = []

    async def __call__(self, planned):
        behavior = self.behaviors.pop(0) if self.behaviors else ("read", 50, ())
        kind, limit, covers = behavior
        await self.adapter.admit()
        if kind == "defer":
            return
        page = await self.runtime.read_inbox(
            limit=limit, tool_arguments={"limit": limit},
        )
        import re as _re
        ids = []
        for block in page["messages"]:
            ids.extend(_re.findall(r'message_id="([^"]+)"', block))
        self.reads.append(ids)
        if covers:
            await self.runtime.store.add_message_covers(
                covers, source="send", by_envelope_id="reply-s",
            )


@pytest.mark.asyncio
async def test_renotice_survives_contribution_from_earlier_generation(
    tmp_path, monkeypatch,
):
    """A renoticed row mentioned by an *earlier* notice must still redeliver.

    Message A is claimed by notice gen N and deferred unread. A later turn
    reads A alongside newer message B (covering only B) while a third
    pending message C stays unread — so pending never drains to zero and
    nothing incidentally wipes the contributions table. A's stale
    contribution from gen N must not filter its redelivery out of every
    future notice for the session.
    """
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "a", 1, content=_human_content("first question"))

    # Turn 1 at the store level: the session claims the notice naming A,
    # then defers without reading (finalize_empty_turn keeps the claim).
    state, items = await store.get_notice_snapshot("provider-1")
    assert [item.envelope_id for item in items] == ["a"]
    await store.start_turn(
        turn_id="turn-defer", provider_session_id="provider-1",
        notice_generation=state.generation,
        notice_message_ids=("a",),
    )
    await store.finalize_empty_turn(turn_id="turn-defer")

    await receipt(store, "b", 2, content=_human_content("second question"))
    await receipt(store, "c", 3, content=_human_content("third question"))
    adapter = Adapter()
    runner = _ScriptedRunner(
        adapter, [("read", 2, ("b",)), ("read", 50, ())],
    )
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=runner, workspace=tmp_path,
    )
    runner.runtime = runtime

    # Turn 2: reads A+B (C stays on the back page), covers only B.
    assert await runtime.process_once()
    assert runner.reads[0] == ["a", "b"]
    row = await store.get_message_by_envelope("a")
    assert row.processing_state is ProcessingState.PENDING
    assert row.renotified

    # Turn 3: the same session must be woken again and redeliver A.
    assert await runtime.process_once()
    assert "a" in runner.reads[1]
    row = await store.get_message_by_envelope("a")
    assert row.processing_state is ProcessingState.PROCESSED


@pytest.mark.asyncio
async def test_unidentified_sender_counts_as_human_for_renotice(
    tmp_path, monkeypatch,
):
    """A row with no sender classification is not exempt from reconciliation."""
    monkeypatch.setenv("PUFFO_COVERS_RENOTICE", "1")
    store = await make_store(tmp_path)
    await receipt(store, "dm1", 1, content={"text": "hey, are you there?"})
    runtime = _build_runtime(store, tmp_path)
    assert await runtime.process_once()
    row = await store.get_message_by_envelope("dm1")
    assert row.processing_state is ProcessingState.PENDING
    assert row.renotified


@pytest.mark.asyncio
async def test_held_send_reports_covers_dropped(tmp_path):
    """Covers on a held send are not recorded, and the result says so."""
    from puffo_agent.agent.global_inbox_runtime import (
        SendAttemptState, TrackingSendDelegate,
    )

    class HoldingCoordinator:
        async def send(self, request=None, **_kwargs):
            return {"state": "held", "reason": "reconsideration"}

    store = await make_store(tmp_path)
    await receipt(store, "q1", 1, content=_human_content("question"))
    runtime = GlobalInboxRuntime(
        store=store, adapter=Adapter(), run_turn=lambda _p: None,
        workspace=tmp_path,
    )
    delegate = TrackingSendDelegate(
        HoldingCoordinator(), SendAttemptState(), runtime=runtime,
    )
    result = await delegate.send(
        {"destination": "ch-1", "text": "answer", "covers": ["q1"]}
    )
    assert result["state"] == "held"
    assert result["covers_recorded"] == []
    assert result["covers_dropped"] == ["q1"]
    assert await store.get_covered_ids(["q1"]) == set()
    await store.close()


@pytest.mark.asyncio
async def test_mark_covered_rejects_blank_ids(tmp_path):
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store, adapter=Adapter(), run_turn=lambda _p: None,
        workspace=tmp_path,
    )
    with pytest.raises(ValueError):
        await runtime.mark_covered(covers=["", "  "])
    await store.close()
