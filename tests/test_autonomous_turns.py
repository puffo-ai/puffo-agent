from __future__ import annotations

from types import SimpleNamespace

import pytest

from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime

from _global_inbox_support import Adapter, make_store, receipt


@pytest.mark.asyncio
async def test_autonomous_provider_turn_is_adopted_and_finalized(tmp_path):
    """A harness background wakeup runs after the daemon turn is finalized.
    Adopting it restores the identity that sends and Inbox reads require."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert runtime.active.turn_id == ""

    adopted = await runtime.adopt_autonomous_turn(
        provider_session_id="provider-1", provider_turn_id="native-turn",
    )
    assert adopted is True
    turn_id = runtime.active.turn_id
    assert turn_id
    assert runtime.active.provider_session_id == "provider-1"
    assert runtime.active.provider_turn_id == "native-turn"
    run = await store.get_turn_run(turn_id)
    assert run is not None and run.state == "in_turn"

    assert await runtime.finish_autonomous_turn() is True
    assert runtime.active.turn_id == ""
    run = await store.get_turn_run(turn_id)
    assert run is not None and run.state == "processed"
    await store.close()


@pytest.mark.asyncio
async def test_autonomous_start_waits_for_daemon_turn_release(tmp_path):
    """A provider start announced after manager terminal fanout can race the
    daemon's durable finalization. Preserve that one-shot start and adopt it
    immediately after the daemon turn releases ownership."""
    store = await make_store(tmp_path)
    adapter = Adapter()
    observed: dict[str, object] = {}

    async def run(planned):
        observed["daemon_turn_id"] = runtime.active.turn_id
        await runtime._on_autonomous_event(
            SimpleNamespace(
                type="turn.autonomous_started",
                data={},
                native_session_id="provider-1",
                native_turn_id="native-autonomous",
            )
        )
        observed["deferred"] = runtime._deferred_autonomous_start is not None
        await adapter.admit()

    await receipt(store, "inbound", 1)
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    runtime._autonomous_ready = True
    assert await runtime.process_once()
    assert observed["deferred"] is True
    daemon_run = await store.get_turn_run(str(observed["daemon_turn_id"]))
    assert daemon_run is not None and daemon_run.state == "processed"
    assert runtime._autonomous_turn_id
    assert runtime.active.turn_id == runtime._autonomous_turn_id
    assert runtime.active.turn_id != observed["daemon_turn_id"]

    await runtime._on_autonomous_event(
        SimpleNamespace(
            type="turn.autonomous_completed",
            data={"outcome": "succeeded"},
        )
    )
    assert runtime.active.turn_id == ""
    await store.close()


@pytest.mark.asyncio
async def test_daemon_turn_queues_behind_an_in_flight_autonomous_run(tmp_path):
    """The provider runs one turn at a time. While it is mid-autonomous-run
    the daemon must queue, not start a turn the driver would reject -- and it
    must pick the work up once that run ends rather than wait for unrelated
    traffic."""
    store = await make_store(tmp_path)
    adapter = Adapter()
    await receipt(store, "inbound", 1)

    async def run(_planned):
        await adapter.admit()

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    adopted_id = runtime.active.turn_id

    # Pending Inbox work exists, but the provider is busy.
    assert await runtime.process_once() is False
    row = await store.get_message_by_envelope("inbound")
    assert row.processing_state == "pending"

    assert await runtime.finish_autonomous_turn() is True
    adopted = await store.get_turn_run(adopted_id)
    assert adopted is not None and adopted.state == "processed"

    # The queued message is now processable.
    assert await runtime.process_once() is True
    await store.close()


@pytest.mark.asyncio
async def test_finish_autonomous_turn_ignores_a_superseded_adoption(tmp_path):
    """If a daemon turn took over, the autonomous terminal frame must not
    clear the daemon's active state -- but the adopted turn's durable row must
    still settle rather than leak in_turn."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    orphan_id = runtime._autonomous_turn_id
    # Simulate a daemon turn replacing the adopted one.
    runtime.active.turn_id = "turn_daemon_owned"

    assert await runtime.finish_autonomous_turn() is True
    assert runtime.active.turn_id == "turn_daemon_owned"
    assert runtime._autonomous_turn_id == ""
    run = await store.get_turn_run(orphan_id)
    assert run is not None and run.state == "requeued"
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_turn_state", "expected_row_state"),
    [("succeeded", "processed", "processed"), ("failed", "requeued", "pending")],
)
async def test_messages_read_during_an_autonomous_run_settle_normally(
    tmp_path, outcome, expected_turn_state, expected_row_state,
):
    """read_inbox during an autonomous run admits real rows into the turn.
    Finalizing it as an empty turn would conflict and strand both the rows
    and the turn in_turn, so the terminal must use the ordinary paths."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    await receipt(store, "read-during-run", 1)
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    turn_id = runtime.active.turn_id

    rows = await store.get_pending()
    selected = tuple(row for row in rows if row.envelope_id == "read-during-run")
    assert selected
    await runtime._admit_inbox_page(
        SimpleNamespace(selected=selected, remaining_count=0),
        snapshot_generation=0,
        requesting_turn_id=turn_id,
        requesting_provider_session_id="p-1",
        requesting_provider_turn_id=None,
    )
    assert runtime.active.message_ids == ["read-during-run"]

    assert await runtime.finish_autonomous_turn(outcome=outcome) is True

    run = await store.get_turn_run(turn_id)
    assert run is not None and run.state == expected_turn_state
    row = await store.get_message_by_envelope("read-during-run")
    assert row.processing_state == expected_row_state
    await store.close()


@pytest.mark.asyncio
async def test_a_single_start_before_recovery_is_replayed_after_it(tmp_path):
    """The provider announces a run once. Adopting before startup recovery
    would let the orphan scan retire a turn that is still executing, but
    dropping the announcement strands it the other way: driver and manager
    keep running a turn this daemon never adopted. Hold it, replay it once
    the barrier lifts, and settle on the single real terminal."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )

    # The one and only start, arriving before recovery.
    await runtime._on_autonomous_event(
        SimpleNamespace(type="turn.autonomous_started", data={},
                        native_session_id="p-1", native_turn_id="n-1"),
    )
    assert runtime.active.turn_id == ""
    assert await store.get_active_turn_runs() == ()

    runtime._stopping = True
    await runtime.run()  # both recovery passes, then the replay

    adopted_id = runtime.active.turn_id
    assert adopted_id, "the held start must be replayed, not dropped"
    run = await store.get_turn_run(adopted_id)
    assert run is not None and run.state == "in_turn"

    # The single real terminal settles it exactly once.
    await runtime._on_autonomous_event(
        SimpleNamespace(type="turn.autonomous_completed",
                        data={"outcome": "succeeded"}),
    )
    run = await store.get_turn_run(adopted_id)
    assert run is not None and run.state == "processed"
    assert runtime.active.turn_id == ""
    await store.close()


@pytest.mark.asyncio
async def test_a_run_that_ended_before_recovery_is_not_adopted(tmp_path):
    """If the terminal also arrived before the barrier the run is over --
    replaying its start would adopt a turn nothing will ever end."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    await runtime._on_autonomous_event(
        SimpleNamespace(type="turn.autonomous_started", data={},
                        native_session_id="p-1", native_turn_id="n-1"),
    )
    await runtime._on_autonomous_event(
        SimpleNamespace(type="turn.autonomous_completed",
                        data={"outcome": "succeeded"}),
    )
    runtime._stopping = True
    await runtime.run()
    assert runtime.active.turn_id == ""
    assert await store.get_active_turn_runs() == ()
    await store.close()


@pytest.mark.asyncio
async def test_autonomous_settle_failure_keeps_the_owner(tmp_path):
    """A failed durable settle must not be reported as a finished turn: the
    rows stay in_turn, so dropping the owner here strands them with nothing
    left in this process to retry before a restart."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime._autonomous_ready = True
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    turn_id = runtime.active.turn_id

    original_finalize = store.finalize_empty_turn

    async def failing_finalize(**_kwargs):
        raise RuntimeError("durable store unavailable")

    store.finalize_empty_turn = failing_finalize  # type: ignore[method-assign]

    assert await runtime.finish_autonomous_turn() is False
    # Ownership survives: the turn is still adopted and still active.
    assert runtime._autonomous_turn_id == turn_id
    assert runtime.active.turn_id == turn_id
    assert runtime.health.state == "degraded"
    run = await store.get_turn_run(turn_id)
    assert run is not None and run.state == "in_turn"

    # Retained is not enough: the manager has released its side and the
    # terminal callback is spent, so the degraded wake is the only thing left
    # that can finish this turn. It must actually retry the settle.
    store.finalize_empty_turn = original_finalize  # type: ignore[method-assign]
    runtime._degraded = False
    runtime._degraded_until = None
    assert await runtime.process_once() is False  # no Inbox work, but...
    run = await store.get_turn_run(turn_id)
    assert run is not None and run.state == "processed"
    assert runtime._autonomous_turn_id == ""
    assert runtime._autonomous_settle_pending is None
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expect_succeeded"), [("succeeded", True), ("abandoned", False)]
)
async def test_autonomous_terminal_settles_status_lifecycle(
    tmp_path, outcome, expect_succeeded,
):
    """read_inbox during the run marks status active; without a terminal here
    that status hangs for the rest of the process."""
    store = await make_store(tmp_path)
    settled: list[dict] = []

    class _Status:
        async def on_turn_active(self, **kwargs):
            settled.append({"event": "active", **kwargs})

        async def on_turn_terminal(self, **kwargs):
            settled.append({"event": "terminal", **kwargs})

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        status_lifecycle=_Status(),
    )
    runtime._autonomous_ready = True
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    turn_id = runtime.active.turn_id

    assert await runtime.finish_autonomous_turn(outcome=outcome) is True
    terminals = [row for row in settled if row["event"] == "terminal"]
    assert len(terminals) == 1
    assert terminals[0]["turn_id"] == turn_id
    assert terminals[0]["succeeded"] is expect_succeeded
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "abandoned"])
async def test_notice_cleanup_failure_resumes_after_terminal_commit(tmp_path, outcome):
    """The row transition can commit before notice cleanup fails. A retry must
    continue from that durable phase instead of replaying the terminal write."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime._autonomous_ready = True
    await receipt(store, "admitted-row", 1)
    assert await runtime.adopt_autonomous_turn(provider_session_id="p-1")
    turn_id = runtime.active.turn_id
    rows = await store.get_pending()
    await runtime._admit_inbox_page(
        SimpleNamespace(selected=rows, remaining_count=0),
        snapshot_generation=0,
        requesting_turn_id=turn_id,
        requesting_provider_session_id="p-1",
        requesting_provider_turn_id=None,
    )

    original = store.release_notice_delivery

    async def boom(*_args, **_kwargs):
        raise RuntimeError("durable store unavailable")

    store.release_notice_delivery = boom  # type: ignore[method-assign]
    assert await runtime.finish_autonomous_turn(outcome=outcome) is False
    assert runtime._autonomous_turn_id == turn_id
    row = await store.get_message_by_envelope("admitted-row")
    assert row.processing_state == ("processed" if outcome == "succeeded" else "pending")
    run = await store.get_turn_run(turn_id)
    assert run is not None
    assert run.state == ("processed" if outcome == "succeeded" else "requeued")

    store.release_notice_delivery = original  # type: ignore[method-assign]
    runtime._degraded = False
    runtime._degraded_until = None
    await runtime.process_once()
    row = await store.get_message_by_envelope("admitted-row")
    assert row.processing_state == ("processed" if outcome == "succeeded" else "pending")
    assert runtime._autonomous_settle_pending is None
    await store.close()


@pytest.mark.asyncio
async def test_adoption_during_notice_planning_queues_instead_of_clobbering(tmp_path):
    """Production wedge (boris, 2026-08-24 15:31): adoption landed while a
    notice turn was mid-planning -- context resolution stretched the window
    between the top-of-loop autonomous guard and _start_local_turn to seconds.
    Admission then overwrote the adopted turn's active binding, the manager
    refused the doomed start, and the adopted row leaked in_turn forever.
    The admission must recheck adoption under the turn-state lock and queue."""
    store = await make_store(tmp_path)
    adapter = Adapter()
    await receipt(store, "inbound", 1)
    started_turns: list[str] = []

    async def run(planned):
        started_turns.append(planned.turn_id)
        await adapter.admit()

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    original_plan = runtime.plan_pending

    async def plan_with_adoption_race(*args, **kwargs):
        planned = await original_plan(*args, **kwargs)
        if planned is not None and not runtime._autonomous_turn_id:
            assert await runtime.adopt_autonomous_turn(
                provider_session_id="provider-1"
            )
        return planned

    runtime.plan_pending = plan_with_adoption_race  # type: ignore[method-assign]

    # The raced cycle must queue: no daemon turn starts, the adopted binding
    # survives, and the inbox row stays pending for the retry.
    assert await runtime.process_once() is False
    adopted_id = runtime._autonomous_turn_id
    assert adopted_id
    assert runtime.active.turn_id == adopted_id
    assert started_turns == []
    row = await store.get_message_by_envelope("inbound")
    assert row.processing_state == "pending"

    # The autonomous terminal settles its row and releases the queue.
    assert await runtime.finish_autonomous_turn() is True
    adopted_run = await store.get_turn_run(adopted_id)
    assert adopted_run is not None and adopted_run.state == "processed"

    # Admission is unblocked: a real daemon turn starts on the retry.
    runtime.plan_pending = original_plan  # type: ignore[method-assign]
    assert await runtime.process_once() is True
    assert started_turns and started_turns[0] != adopted_id
    await store.close()


@pytest.mark.asyncio
async def test_orphaned_adopted_turn_settles_its_row_on_terminal(tmp_path):
    """Defense in depth for the same wedge: if the adopted turn ever loses its
    active binding anyway, the terminal must still settle the durable row.
    A row left in_turn reads as "agent busy" to the scheduler -- notices arm
    but never come due, permanently."""
    store = await make_store(tmp_path)
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert await runtime.adopt_autonomous_turn(provider_session_id="provider-1")
    orphan_id = runtime._autonomous_turn_id
    # Simulate the binding being lost to another writer.
    runtime.active.clear()

    assert await runtime.finish_autonomous_turn(outcome="succeeded") is True
    assert runtime._autonomous_turn_id == ""
    assert runtime._autonomous_settle_pending is None
    run = await store.get_turn_run(orphan_id)
    assert run is not None and run.state == "requeued"

    # Admission is unblocked: a daemon turn starts on pending work afterwards.
    await receipt(store, "inbound", 1)
    started_turns: list[str] = []

    async def run_turn(planned):
        started_turns.append(planned.turn_id)
        await adapter.admit()

    runtime.run_turn = run_turn  # type: ignore[method-assign]
    assert await runtime.process_once() is True
    assert started_turns
    await store.close()


@pytest.mark.asyncio
async def test_orphan_settle_releases_notice_delivery_for_the_session(tmp_path):
    """P1 of the orphan-settle review: a busy notice can mark pending rows
    delivered against the orphan's provider session. Requeueing the turn row
    alone leaves that evidence in place and the same session never sees the
    rows as candidates again. The settle must release notice delivery, like
    crash recovery does."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    await receipt(store, "starved-row", 1)
    assert await runtime.adopt_autonomous_turn(provider_session_id="provider-1")
    orphan_id = runtime._autonomous_turn_id

    state, candidates = await store.get_notice_snapshot("provider-1")
    assert [row.envelope_id for row in candidates] == ["starved-row"]
    assert await store.mark_notice_delivered(
        state.generation, "provider-1", ("starved-row",), turn_id=orphan_id
    )
    assert await store.get_notice_candidates("provider-1") == ()

    runtime.active.clear()
    assert await runtime.finish_autonomous_turn(outcome="succeeded") is True
    run = await store.get_turn_run(orphan_id)
    assert run is not None and run.state == "requeued"
    # The same provider session sees the row as a candidate again.
    candidates = await store.get_notice_candidates("provider-1")
    assert [row.envelope_id for row in candidates] == ["starved-row"]
    await store.close()


@pytest.mark.asyncio
async def test_orphan_settle_terminates_the_status_lifecycle(tmp_path):
    """P2 of the orphan-settle review: a mid-run read_inbox admission marks
    worker status active for the orphan turn. The settle must emit exactly one
    terminal from the durable ids -- never from the current active state,
    which belongs to whichever writer took the binding."""
    store = await make_store(tmp_path)
    settled: list[dict] = []

    class _Status:
        async def on_turn_active(self, **kwargs):
            settled.append({"event": "active", **kwargs})

        async def on_turn_terminal(self, **kwargs):
            settled.append({"event": "terminal", **kwargs})

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        status_lifecycle=_Status(),
    )
    await receipt(store, "admitted-row", 1)
    assert await runtime.adopt_autonomous_turn(provider_session_id="provider-1")
    orphan_id = runtime._autonomous_turn_id
    rows = await store.get_pending()
    await runtime._admit_inbox_page(
        SimpleNamespace(selected=rows, remaining_count=0),
        snapshot_generation=0,
        requesting_turn_id=orphan_id,
        requesting_provider_session_id="provider-1",
        requesting_provider_turn_id=None,
    )

    runtime.active.clear()
    assert await runtime.finish_autonomous_turn(outcome="succeeded") is True
    terminals = [row for row in settled if row["event"] == "terminal"]
    assert len(terminals) == 1
    assert terminals[0]["turn_id"] == orphan_id
    assert terminals[0]["succeeded"] is False
    assert tuple(terminals[0]["message_ids"]) == ("admitted-row",)
    row = await store.get_message_by_envelope("admitted-row")
    assert row.processing_state == "pending"
    await store.close()


@pytest.mark.asyncio
async def test_orphan_settle_keeps_the_owner_on_unexpected_row_state(tmp_path):
    """Strictness: a missing or unexpectedly-stated row must keep the
    autonomous owner and go to settle-pending retry -- clearing it silently
    is the exact antipattern that produced the wedge."""
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert await runtime.adopt_autonomous_turn(provider_session_id="provider-1")
    orphan_id = runtime._autonomous_turn_id
    runtime.active.clear()

    original_get = store.get_turn_run

    async def missing(_turn_id):
        return None

    store.get_turn_run = missing  # type: ignore[method-assign]
    assert await runtime.finish_autonomous_turn(outcome="succeeded") is False
    assert runtime._autonomous_turn_id == orphan_id
    assert runtime._autonomous_settle_pending == "succeeded"

    async def unexpected(turn_id):
        run = await original_get(turn_id)
        assert run is not None
        return SimpleNamespace(
            turn_id=run.turn_id,
            provider_session_id=run.provider_session_id,
            state="processed",
            message_ids=run.message_ids,
        )

    store.get_turn_run = unexpected  # type: ignore[method-assign]
    runtime._degraded = False
    runtime._degraded_until = None
    assert await runtime.finish_autonomous_turn(outcome="succeeded") is False
    assert runtime._autonomous_turn_id == orphan_id

    # With the store healthy again the retry settles and releases the owner.
    store.get_turn_run = original_get  # type: ignore[method-assign]
    runtime._degraded = False
    runtime._degraded_until = None
    assert await runtime.finish_autonomous_turn(outcome="succeeded") is True
    assert runtime._autonomous_turn_id == ""
    run = await store.get_turn_run(orphan_id)
    assert run is not None and run.state == "requeued"
    await store.close()
