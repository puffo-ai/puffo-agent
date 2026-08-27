"""Durable provider-session transitions across a global Inbox API retry.

A notice turn carries no bodies: the model must call ``read_inbox``, which
durably admits the selected rows under the *current* provider session. When a
retryable provider error is followed by a failed resume, the adapter starts a
replacement session and sends ``planned.provider_input`` as its fallback. These
tests pin the two facts that make such a replacement safe: it receives the exact
previously admitted bodies, and it owns them under a durable ``turn_runs``
transition before any row can be marked processed.
"""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.message_store import LifecycleConflict, ProcessingState

from test_global_inbox_runtime import Adapter, make_store, receipt

SESSION_A = "provider-1"
SESSION_B = "replacement-session"
SESSION_C = "concurrent-session"
BODY = "the exact admitted body a replacement session must receive"


class _RetrySessionAdapter(Adapter):
    """Correlate the initial provider turn and its replacement retry."""


class _ReadThenRetryRunner:
    """Read one row under session A, fail once, then retry in a given session."""

    def __init__(self, store, adapter, *, retry_session, before_retry_admit=None):
        self.store = store
        self.adapter = adapter
        self.retry_session = retry_session
        self.before_retry_admit = before_retry_admit
        self.runtime = None
        self.retry_inputs = []
        self.owner_at_retry = []
        self.read_bodies = []
        self.read_turn_id = ""

    async def __call__(self, planned):
        self.read_turn_id = planned.turn_id
        await self.adapter.admit(
            session=self.adapter.session, provider_turn_id="provider-turn-1",
        )
        page = await self.runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        assert len(page["messages"]) == 1
        self.read_bodies.append(page["messages"][0])
        raise AgentAPIError("rate limit", is_auth=False)

    async def handle_global_inbox_retry(self, planned):
        self.retry_inputs.append(planned.provider_input)
        if self.before_retry_admit is not None:
            await self.before_retry_admit(self)
        self.adapter.session = self.retry_session
        await self.adapter.admit(
            session=self.retry_session, provider_turn_id="provider-turn-2",
        )
        # Sampled before the provider turn returns success: completion may only
        # follow a durable owner that already matches the answering session.
        run = await self.store.get_turn_run(planned.turn_id)
        self.owner_at_retry.append(run.provider_session_id)
        return None


async def _retry_runtime(tmp_path, *, retry_session, before_retry_admit=None):
    store = await make_store(tmp_path)
    await receipt(store, "inbox-row", 1, content=BODY)
    adapter = _RetrySessionAdapter()
    runner = _ReadThenRetryRunner(
        store,
        adapter,
        retry_session=retry_session,
        before_retry_admit=before_retry_admit,
    )
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
        max_api_retries=2,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )
    runner.runtime = runtime
    return store, adapter, runner, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_session", [False, True])
async def test_fresh_session_retry_delivers_exact_admitted_bodies(
    tmp_path, fresh_session,
):
    """A replacement session gets the admitted bodies and durable ownership."""
    retry_session = SESSION_B if fresh_session else SESSION_A
    store, _adapter, runner, runtime = await _retry_runtime(
        tmp_path, retry_session=retry_session,
    )

    assert await runtime.process_once() is True

    assert len(runner.retry_inputs) == 1
    retry_input = runner.retry_inputs[0]
    # The rebuilt payload is the exact durable turn, not the bodyless notice.
    assert retry_input.startswith("<global_inbox_turn>")
    assert retry_input.endswith("</global_inbox_turn>")
    assert BODY in retry_input
    assert "<global_inbox_notice>" not in retry_input
    assert runner.read_bodies[0] in retry_input
    # Durable ownership moved with the bodies, before completion.
    assert runner.owner_at_retry == [retry_session]
    run = await store.get_turn_run(runner.read_turn_id)
    assert run.provider_session_id == retry_session
    assert run.state == ProcessingState.PROCESSED.value
    row = await store.get_message_by_envelope("inbox-row")
    assert row.processing_state is ProcessingState.PROCESSED
    assert list(await store.get_pending()) == []
    await store.close()


@pytest.mark.asyncio
async def test_failed_session_transition_processes_nothing(tmp_path):
    """A lost ownership race finalizes nothing and leaves the row recoverable."""

    async def steal_ownership(runner):
        # A concurrent owner change between attempt start and admission: the
        # transition is a compare-and-set on the durable row, so the retry's
        # A -> B transfer must lose rather than overwrite.
        await runner.store.transfer_turn_session(
            runner.read_turn_id,
            from_provider_session_id=SESSION_A,
            to_provider_session_id=SESSION_C,
        )

    store, _adapter, runner, runtime = await _retry_runtime(
        tmp_path, retry_session=SESSION_B, before_retry_admit=steal_ownership,
    )

    assert await runtime.process_once() is True

    assert runner.owner_at_retry == []
    run = await store.get_turn_run(runner.read_turn_id)
    # Never owned by the replacement session, and no row was finalized.
    assert run.provider_session_id == SESSION_C
    assert run.state == "requeued"
    row = await store.get_message_by_envelope("inbox-row")
    assert row.processing_state is ProcessingState.PENDING
    assert row.processed_at is None
    assert [item.envelope_id for item in await store.get_pending()] == ["inbox-row"]
    assert runtime.health.state == "degraded"
    await store.close()


@pytest.mark.asyncio
async def test_empty_notice_retry_moves_delivery_to_replacement_session(tmp_path):
    """A successful replacement must not wake again for the same notice IDs."""
    store = await make_store(tmp_path)
    await receipt(store, "unread-row", 1)
    adapter = _RetrySessionAdapter()

    class Runner:
        async def __call__(self, _planned):
            await adapter.admit(session=SESSION_A, provider_turn_id="turn-1")
            raise AgentAPIError("rate limit", is_auth=False)

        async def handle_global_inbox_retry(self, _planned):
            adapter.session = SESSION_B
            await adapter.admit(session=SESSION_B, provider_turn_id="turn-2")

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=Runner(),
        workspace=tmp_path,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )

    assert await runtime.process_once()
    state = await store.get_notice_state()
    assert state.last_delivered_provider_session_id == SESSION_B
    assert not await store.get_notice_candidates(SESSION_B)
    assert not await runtime.process_once()
    await store.close()


async def _seed_transfer_store(tmp_path):
    store = await make_store(tmp_path)
    for index, envelope_id in enumerate(("row-1", "row-2", "row-3"), start=1):
        await receipt(store, envelope_id, index)
    await store.admit_messages(
        ["row-1"], turn_id="turn-1", provider_session_id=SESSION_A,
    )
    return store


async def _assert_transfer_success(store):
    run = await store.transfer_turn_session(
        "turn-1",
        from_provider_session_id=SESSION_A,
        to_provider_session_id=SESSION_B,
    )
    assert run.provider_session_id == SESSION_B
    assert run.state == ProcessingState.IN_TURN.value
    rows = await store.get_in_turn_messages("turn-1", SESSION_B)
    assert [row.envelope_id for row in rows] == ["row-1"]
    assert await store.get_in_turn_messages("turn-1", SESSION_A) == ()
    admitted = await store.admit_messages(
        ["row-2"], turn_id="turn-1", provider_session_id=SESSION_B,
    )
    assert admitted.message_ids == ("row-1", "row-2")
    with pytest.raises(LifecycleConflict, match="another provider session"):
        await store.admit_messages(
            ["row-3"], turn_id="turn-1", provider_session_id=SESSION_A,
        )


async def _assert_transfer_rejected(store, case):
    if case == "not_in_turn":
        await store.mark_processed(["row-1"], turn_id="turn-1")
    arguments = {
        "wrong_from": ("turn-1", "some-other-session", SESSION_B),
        "not_in_turn": ("turn-1", SESSION_A, SESSION_B),
        "unknown_turn": ("turn-absent", SESSION_A, SESSION_B),
        "empty_to": ("turn-1", SESSION_A, ""),
    }[case]
    turn_id, from_session, to_session = arguments
    with pytest.raises(LifecycleConflict):
        await store.transfer_turn_session(
            turn_id,
            from_provider_session_id=from_session,
            to_provider_session_id=to_session,
        )
    run = await store.get_turn_run("turn-1")
    assert run.provider_session_id == SESSION_A


async def _assert_non_owner_completion_rejected(store):
    with pytest.raises(LifecycleConflict, match="another provider session"):
        await store.mark_processed(
            ["row-1"], turn_id="turn-1", provider_session_id=SESSION_B,
        )
    row = await store.get_message_by_envelope("row-1")
    assert row.processing_state is ProcessingState.IN_TURN
    run = await store.get_turn_run("turn-1")
    assert run.state == ProcessingState.IN_TURN.value
    completed = await store.mark_processed(
        ["row-1"], turn_id="turn-1", provider_session_id=SESSION_A,
    )
    assert completed.state == ProcessingState.PROCESSED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "wrong_from",
        "not_in_turn",
        "unknown_turn",
        "empty_to",
        "non_owner_completion",
    ],
)
async def test_turn_session_transfer_store_contract(tmp_path, case):
    """The store owns the only sanctioned session transition and its gate."""
    store = await _seed_transfer_store(tmp_path)
    if case == "success":
        await _assert_transfer_success(store)
    elif case == "non_owner_completion":
        await _assert_non_owner_completion_rejected(store)
    else:
        await _assert_transfer_rejected(store, case)
    await store.close()
