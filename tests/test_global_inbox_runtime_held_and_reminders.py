"""Held recovery, reminders, and crash recovery runtime coverage."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from puffo_agent.agent.context_controller import (
    DecisionOutcome,
    ProviderAdmissionEvent,
)
from puffo_agent.agent.global_inbox_runtime import (
    ActiveBoundaryAdapter,
    ActiveExactUnion,
    GlobalInboxRuntime,
    MessageRoute,
    TrackingSendDelegate,
    format_stored_message,
    route_for,
)
from puffo_agent.agent.inbox_scheduler import (
    InboxNoticeDelivery,
    NoticeDeliveryCapability,
)
from puffo_agent.agent.message_store import (
    MessageStore,
    ProcessingState,
    StoredMessage,
)
from puffo_agent.agent.reminder_sync import (
    REMINDER_PAYLOAD_FORMAT,
    ReminderSync,
    _base64url_encode,
    encrypt_reminder_payload,
)
from puffo_agent.agent.runtime_event_outbox import RuntimeEventOutbox
from puffo_agent.agent.shared_content import INBOX_RESPONSE_DECISION_CUE
from puffo_agent.crypto.keystore import KeyStore

from _global_inbox_support import _run_prior_context_delivery_case
from test_global_inbox_runtime import (
    Adapter,
    ScriptedContext,
    ToolReturnAdapter,
    make_store,
    projection_metadata,
    receipt,
    runtime_events,
)


@pytest.mark.asyncio
async def test_held_sync_fails_closed_when_bounded_semantic_context_overflows(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1",
    )
    for seq in range(2, 53):
        await receipt(store, f"held-{seq}", seq, content="small")
    adapter = ToolReturnAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"
    rows = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 52, "held-52", "provider-1",
    )
    assert rows == ()
    staged = runtime.held[("sp-1", "ch-1")]
    assert staged.synchronized is False
    assert staged.message_ids == ()
    assert "exceeds" in staged.diagnostic
    assert [row.envelope_id for row in await store.get_pending()] == [
        f"held-{seq}" for seq in range(2, 53)
    ]
    first = await runtime.read_inbox(
        target="channel:sp-1:ch-1", limit=50,
    )
    assert first["has_more"] is True
    assert len(first["messages"]) == 50
    assert await ActiveBoundaryAdapter(
        store, runtime.active
    ).get_active_turn_through_seq("sp-1", "ch-1") == 51

    second = await runtime.read_inbox(
        target="channel:sp-1:ch-1",
        cursor=first["next_cursor"],
        limit=50,
    )
    assert second["has_more"] is False
    assert len(second["messages"]) == 1
    assert "held-52" in second["messages"][0]
    assert await ActiveBoundaryAdapter(
        store, runtime.active
    ).get_active_turn_through_seq("sp-1", "ch-1") == 52
    await store.close()


@pytest.mark.asyncio
async def test_held_sync_overflow_is_independent_of_formatter_and_context_budget(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1",
    )
    for seq in range(2, 53):
        await receipt(store, f"held-{seq}", seq, content="small")
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        formatter=lambda _row: "x" * 20_000,
        estimator=lambda _text: 1,
    )
    runtime.active.turn_id = "turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"

    rows = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 52, "held-52", "provider-1",
    )

    assert rows == ()
    staged = runtime.held[("sp-1", "ch-1")]
    assert staged.message_ids == ()
    assert staged.synchronized is False
    assert "exceeds" in staged.diagnostic
    await store.close()


@pytest.mark.asyncio
async def test_repeated_held_sync_proof_returns_stable_local_semantic_context(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1)
    await receipt(store, "held", 2)
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1",
    )

    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"

    first = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 2, "held", "provider-1",
    )
    second = await runtime.held_recovery_source.query_held_messages(
        "sp-1", "ch-1", 2, "held", "provider-1",
    )
    assert first == second
    assert first[0]["content"] == "text-held"
    assert runtime.held[("sp-1", "ch-1")].message_ids == ("held",)

    in_turn = await store.get_in_turn_messages("turn", "provider-1")
    assert [row.envelope_id for row in in_turn] == ["initial"]
    assert runtime.active.message_ids == ["initial"]
    assert [row.envelope_id for row in await store.get_pending()] == ["held"]
    await store.close()


@pytest.mark.asyncio
async def test_notice_then_correlated_read_admits_and_processes_exact_page(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "pull-1", 1)
    await receipt(store, "pull-2", 2)

    class PullAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.continuation = None
            self.continuation_key = ""

        def register_continuation_callback(
            self, callback, planning_cycle_key, **_kwargs
        ):
            self.continuation = callback
            self.continuation_key = planning_cycle_key

        async def admit_continuation(self):
            callback, self.continuation = self.continuation, None
            await callback(ProviderAdmissionEvent(
                planning_cycle_key=self.continuation_key,
                provider_session_id=self.session,
                provider_turn_id="provider-turn",
                tool_call_id="tool-1",
                admitted_at=datetime.now(timezone.utc),
            ))

    adapter = PullAdapter()

    async def run(planned):
        assert "text-pull" not in planned.provider_input
        await adapter.admit()
        # A metadata-only notice has no plaintext rows to register.
        assert runtime.active.visible_message_ids == []
        page = await runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        assert len(page["messages"]) == 1
        assert [row.envelope_id for row in await store.get_pending()] == [
            "pull-1", "pull-2"
        ]
        await adapter.admit_continuation()
        assert runtime.active.visible_message_ids == ["pull-1"]
        assert [row.envelope_id for row in await store.get_pending()] == ["pull-2"]

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path
    )
    assert await runtime.process_once()
    assert (await store.get_message_by_envelope("pull-1")).processing_state is (
        ProcessingState.PROCESSED
    )
    assert (await store.get_message_by_envelope("pull-2")).processing_state is (
        ProcessingState.PENDING
    )
    await store.close()


def test_format_stored_message_marks_only_runtime_identity_aliases(tmp_path):
    def stored(envelope_id, sender):
        return StoredMessage(
            envelope_id=envelope_id,
            envelope_kind="channel",
            sender_slug=sender,
            channel_id="ch-1",
            space_id="sp-1",
            recipient_slug=None,
            content_type="text/plain",
            content="same body regardless of sender",
            sent_at=1,
            received_at=1,
            server_seq=1,
        )

    human = stored("human", "human")
    peer = stored("peer", "peer-agent")
    self_echo = stored("self", "@Wire-Agent")
    self_echo.thread_root_id = None

    assert projection_metadata(format_stored_message(human))["is_self"] is False
    assert projection_metadata(format_stored_message(peer))["is_self"] is False
    assert projection_metadata(format_stored_message(self_echo))["is_self"] is False
    assert projection_metadata(
        format_stored_message(self_echo, current_agent_aliases=("wire-agent",))
    )["is_self"] is True
    rendered = format_stored_message(self_echo, current_agent_aliases=("wire-agent",))
    assert 'target_type="channel"' in rendered
    assert 'target_type="thread"' not in rendered
    assert "thread_root_id=None" not in rendered
    assert 'sender_type="agent"' in rendered
    assert projection_metadata(rendered)["is_self"] is True

    runtime = GlobalInboxRuntime(
        store=SimpleNamespace(),
        adapter=SimpleNamespace(slug="wire-agent"),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        send_mode_keys=("runtime-agent-id",),
    )
    assert projection_metadata(runtime.formatter(self_echo))["is_self"] is True
    assert projection_metadata(runtime.formatter(stored("alias", "runtime-agent-id")))[
        "is_self"
    ] is True
    assert projection_metadata(runtime.formatter(human))["is_self"] is False

    no_identity = GlobalInboxRuntime(
        store=SimpleNamespace(),
        adapter=SimpleNamespace(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert projection_metadata(no_identity.formatter(self_echo))["is_self"] is False

    custom_calls = []

    def custom_formatter(item):
        custom_calls.append(item.envelope_id)
        return f"custom:{item.envelope_id}"

    custom = GlobalInboxRuntime(
        store=SimpleNamespace(),
        adapter=SimpleNamespace(slug="wire-agent"),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        formatter=custom_formatter,
        agent_id="wire-agent",
    )
    assert custom.formatter(self_echo) == "custom:self"
    assert custom_calls == ["self"]


@pytest.mark.asyncio
async def test_peer_progress_turn_receives_grounded_prior_context(tmp_path):
    result = await _run_prior_context_delivery_case(tmp_path)
    assert result["provider_turns"] == 2
    assert result["transport_calls"] == 1
    assert result["peer_state"] is ProcessingState.PROCESSED
    second_input = json.dumps(result["provider_inputs"][1], sort_keys=True)
    for expected in (
        result["human_body"],
        result["first_contribution"],
        result["peer_body"],
    ):
        assert expected in second_input
    assert result["self_metadata"]["sender_slug"] == "agent"
    assert result["self_metadata"]["is_self"] is True
    assert result["prior_ids"] == ["human-origin", "agent-contribution"]
    assert result["future_state"] is ProcessingState.PENDING


@pytest.mark.asyncio
async def test_read_inbox_prior_context_preserves_paging_and_exact_admission(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(
        store,
        "prior-page-context",
        1,
        content="durable prior context",
    )
    await store.admit_messages(
        ["prior-page-context"],
        turn_id="prior-page-context-turn",
        provider_session_id="provider-1",
    )
    await store.mark_processed(
        ["prior-page-context"], turn_id="prior-page-context-turn"
    )
    await receipt(store, "page-context-1", 2, content="page one")
    await receipt(store, "page-context-2", 3, content="page two")
    await store.start_turn(
        turn_id="paging-turn",
        provider_session_id="provider-1",
    )
    adapter = ToolReturnAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "paging-turn"
    runtime.active.provider_session_id = adapter.session
    runtime.active.provider_turn_id = "provider-turn"

    first = await runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
    second = await runtime.read_inbox(
        cursor=first["next_cursor"],
        limit=1,
        tool_arguments={"cursor": first["next_cursor"], "limit": 1},
    )
    assert "page one" in first["messages"][0]
    assert "page two" in second["messages"][0]
    assert [
        projection_metadata(block)["envelope_id"]
        for block in first["prior_context"]
    ] == ["prior-page-context"]
    assert [
        projection_metadata(block)["envelope_id"]
        for block in second["prior_context"]
    ] == ["prior-page-context"]
    assert first["has_more"] is True
    assert second["has_more"] is False
    assert first["prior_context_has_more"] is False
    assert second["prior_context_has_more"] is False
    assert second["next_cursor"] == ""
    assert runtime.active.message_ids == ["page-context-1", "page-context-2"]
    assert runtime.active.visible_message_ids == [
        "page-context-1", "prior-page-context", "page-context-2",
    ]
    prior = await store.get_message_by_envelope("prior-page-context")
    page_rows = await asyncio.gather(
        store.get_message_by_envelope("page-context-1"),
        store.get_message_by_envelope("page-context-2"),
    )
    assert prior is not None and prior.processing_state is ProcessingState.PROCESSED
    assert all(
        row is not None and row.processing_state is ProcessingState.IN_TURN
        for row in page_rows
    )
    await store.mark_processed(
        ["page-context-1", "page-context-2"], turn_id="paging-turn"
    )
    await store.close()


@pytest.mark.asyncio
async def test_correlated_read_processes_sequence_less_local_runtime_event(tmp_path):
    store = await make_store(tmp_path)
    await store.store_local_event(
        {
            "envelope_id": "intro-prompt-ch-1",
            "envelope_kind": "channel",
            "sender_slug": "system",
            "channel_id": "ch-1",
            "space_id": "sp-1",
            "content": "introduce yourself",
            "sent_at": 1,
            "thread_root_id": "intro-prompt-ch-1",
        },
        reason="channel introduction",
        intro_channel_id="ch-1",
    )

    class PullAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.continuation = None
            self.continuation_key = ""

        def register_continuation_callback(
            self, callback, planning_cycle_key, **_kwargs
        ):
            self.continuation = callback
            self.continuation_key = planning_cycle_key

        async def admit_continuation(self):
            callback, self.continuation = self.continuation, None
            await callback(ProviderAdmissionEvent(
                planning_cycle_key=self.continuation_key,
                provider_session_id=self.session,
                provider_turn_id="provider-turn",
                tool_call_id="tool-local",
                admitted_at=datetime.now(timezone.utc),
            ))

    adapter = PullAdapter()

    async def run(planned):
        await adapter.admit()
        page = await runtime.read_inbox(limit=50, tool_arguments={})
        assert len(page["messages"]) == 1
        assert "introduce yourself" in page["messages"][0]
        await adapter.admit_continuation()
        route = runtime.resolve_active_send_route(
            "ch-1", {"root_id": ""}, {}
        )
        assert route is not None
        assert route.kind == "channel"
        assert route.thread_root_id == ""

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path
    )
    assert await runtime.process_once()
    intro = await store.get_message_by_envelope("intro-prompt-ch-1")
    assert intro is not None
    assert intro.server_seq is None
    assert intro.processing_state is ProcessingState.PROCESSED
    await store.close()


@pytest.mark.asyncio
async def test_only_intro_system_anchor_authorizes_top_level_channel_send(tmp_path):
    store = await make_store(tmp_path)
    await store.store_local_event(
        {
            "envelope_id": "membership-joined-ch-1-agent-1-event-1",
            "envelope_kind": "channel",
            "sender_slug": "system",
            "channel_id": "ch-1",
            "space_id": "sp-1",
            "content": "membership update",
            "sent_at": 1,
            "thread_root_id": "membership-joined-ch-1-agent-1-event-1",
        },
        reason="membership system message",
    )
    row = await store.get_message_by_envelope(
        "membership-joined-ch-1-agent-1-event-1"
    )
    assert row is not None
    route = route_for(row)
    assert route.kind == "thread"
    assert route.thread_root_id == row.envelope_id
    await store.close()


@pytest.mark.asyncio
async def test_initial_and_busy_notices_are_complete_content_free_inputs(tmp_path):
    plaintext = "plaintext-notice-sentinel"
    attachment = "attachment-content-sentinel"
    store = await make_store(tmp_path)
    await receipt(store, "notice-1", 8, content=f"{plaintext}:{attachment}")

    class BusyAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.offers = []
            self.accept = True

        async def offer_inbox_notice(self, turn_id, provider_input):
            self.offers.append((turn_id, provider_input))
            return self.accept

    adapter = BusyAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        notice_delivery=InboxNoticeDelivery(NoticeDeliveryCapability.DIRECT),
    )
    initial = await runtime.plan_pending()
    assert initial is not None
    initial_serialized = json.dumps({
        "provider_input": initial.provider_input,
        "message_ids": initial.message_ids,
        "formatted_blocks": initial.formatted_blocks,
    })
    assert initial.message_ids == ()
    assert initial.formatted_blocks == ()
    assert '"generation":' in initial.provider_input
    assert '"message_count":1' in initial.provider_input
    assert '"latest_seq":8' in initial.provider_input
    assert '"version":3' in initial.provider_input
    assert '"content_included":false' in initial.provider_input
    assert '"read_tool":"read_inbox"' in initial.provider_input
    assert "channel:sp-1:ch-1" in initial.provider_input
    assert initial.provider_input.endswith(INBOX_RESPONSE_DECISION_CUE)
    assert "decide-response" in initial.provider_input
    assert plaintext not in initial_serialized
    assert attachment not in initial_serialized

    runtime.active.turn_id = "active-turn"
    runtime.active.provider_session_id = "provider-1"
    assert await runtime.offer_busy_notice(turn_id="active-turn")
    busy_serialized = json.dumps(adapter.offers)
    assert plaintext not in busy_serialized
    assert attachment not in busy_serialized
    state = await store.get_notice_state()
    assert state.last_delivered_generation == state.generation
    assert state.last_delivered_provider_session_id == "provider-1"
    assert not state.is_due_for("provider-1")
    await store.close()


@pytest.mark.asyncio
async def test_rejected_or_stale_busy_notice_retains_generation(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "notice-reject", 9)

    class RejectingAdapter(Adapter):
        async def offer_inbox_notice(self, _turn_id, _provider_input):
            return False

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=RejectingAdapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        notice_delivery=InboxNoticeDelivery(NoticeDeliveryCapability.DIRECT),
    )
    runtime.active.turn_id = "active-turn"
    runtime.active.provider_session_id = "provider-1"
    before = await store.get_notice_state()
    assert not await runtime.offer_busy_notice(turn_id="stale-turn")
    assert not await runtime.offer_busy_notice(turn_id="active-turn")
    after = await store.get_notice_state()
    assert after.generation == before.generation
    assert after.last_delivered_generation == before.last_delivered_generation
    assert after.delivery_pending
    assert [row.envelope_id for row in await store.get_pending()] == [
        "notice-reject"
    ]
    await store.close()


@pytest.mark.asyncio
async def test_notice_turn_without_correlated_read_rearms_for_same_session(
    tmp_path,
):
    """Durable pending work is re-notified on the same warm provider session.

    A Turn that admitted no rows is not an Inbox ACK, so the unchanged pending
    set must not stay suppressed until unrelated ingress arrives.
    """
    store = await make_store(tmp_path)
    await receipt(store, "notice-unread", 10)
    adapter = Adapter()
    calls = 0

    async def run_turn(_planned):
        nonlocal calls
        calls += 1
        await adapter.admit()

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run_turn,
        workspace=tmp_path,
    )
    before = await store.get_notice_state()

    assert await runtime.process_once()

    after = await store.get_notice_state()
    assert [row.envelope_id for row in await store.get_pending()] == [
        "notice-unread"
    ]
    assert after.generation == before.generation + 1
    assert after.last_delivered_generation == before.generation
    assert after.last_delivered_provider_session_id == adapter.session
    assert after.is_due_for(adapter.session)
    assert await runtime.process_once()
    assert calls == 2
    assert runtime._degraded is False
    await store.close()


@pytest.mark.asyncio
async def test_notice_restart_redelivers_same_session_and_rediscovers_once_for_replacement(
    tmp_path,
):
    def described_pending(provider_input):
        """Return the notice body without its per-delivery generation."""
        summary = json.loads(provider_input.split("\n", 2)[1])
        summary.pop("generation")
        return summary

    store = await make_store(tmp_path)
    await receipt(store, "session-notice", 10)
    first_adapter = Adapter()
    first_calls = []

    async def first_run(planned):
        first_calls.append(planned.provider_input)
        await first_adapter.admit(first_adapter.session)

    first = GlobalInboxRuntime(
        store=store,
        adapter=first_adapter,
        run_turn=first_run,
        workspace=tmp_path,
    )
    assert await first.process_once()
    accepted = await store.get_notice_state()
    # The Turn admitted no rows, so the same session stays eligible for a
    # redelivery rather than stranding the still-pending row.
    assert accepted.is_due_for(first_adapter.session)
    assert len(first_calls) == 1

    resumed_calls = []

    async def resumed_run(planned):
        resumed_calls.append(planned.provider_input)
        await first_adapter.admit(first_adapter.session)

    resumed = GlobalInboxRuntime(
        store=store,
        adapter=first_adapter,
        run_turn=resumed_run,
        workspace=tmp_path,
    )
    assert await resumed.process_once()
    assert len(resumed_calls) == 1
    assert described_pending(resumed_calls[0]) == described_pending(first_calls[0])

    replacement_adapter = Adapter()
    replacement_adapter.session = "provider-2"
    replacement_calls = []

    async def replacement_run(planned):
        replacement_calls.append(planned.provider_input)
        await replacement_adapter.admit(replacement_adapter.session)

    replacement = GlobalInboxRuntime(
        store=store,
        adapter=replacement_adapter,
        run_turn=replacement_run,
        workspace=tmp_path,
    )
    assert await replacement.process_once()
    assert len(replacement_calls) == 1
    assert described_pending(replacement_calls[0]) == described_pending(first_calls[0])
    final = await store.get_notice_state()
    assert final.last_delivered_provider_session_id == "provider-2"
    assert [row.envelope_id for row in await store.get_pending()] == [
        "session-notice"
    ]
    await store.close()


@pytest.mark.asyncio
async def test_mixed_message_and_due_reminder_share_one_notice_read_and_turn(tmp_path):
    store = await make_store(tmp_path)
    ordinary_body = "ordinary message body remains unchanged"
    await receipt(store, "ordinary", 1, content=ordinary_body)
    reminder = await store.create_reminder(
        content="exact Agent-authored reminder content",
        target="channel:sp-1:ch-1",
        intended_at_ms=1,
    )
    await store.deliver_due_reminders(now_ms=2)

    adapter = ToolReturnAdapter()
    observed: dict[str, object] = {}

    async def run(planned):
        assert ordinary_body not in planned.provider_input
        assert "exact Agent-authored reminder content" not in planned.provider_input
        summary = json.loads(
            planned.provider_input.split("\n", 2)[1]
        )
        assert summary["message_count"] == 2
        assert summary["targets"] == [{"target": "channel:sp-1:ch-1", "count": 2}]
        await adapter.admit()
        observed["page"] = await runtime.read_inbox(limit=50)
        observed["ids"] = tuple(runtime.active.message_ids)

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    assert await runtime.process_once()
    page = observed["page"]
    assert observed["ids"] == (
        "ordinary", f"reminder-occurrence:{reminder.occurrence_id}",
    )
    assert len(page["messages"]) == 2
    ordinary, reminder_block = page["messages"]
    assert ordinary_body in ordinary
    assert "[message context_version=1 seq=1" in ordinary
    assert 'event_type="reminder"' in reminder_block
    for expected in (
        reminder.reminder_id,
        reminder.occurrence_id,
        "channel:sp-1:ch-1",
        "exact Agent-authored reminder content",
        "intended_at=\"1970-01-01T00:00:00.001Z\"",
        "actual_fire_at=\"1970-01-01T00:00:00.002Z\"",
    ):
        assert expected in reminder_block
    assert all(
        forbidden not in reminder_block.lower()
        for forbidden in ("execute", "skip", "apologize", "reply", "silence")
    )
    notice = (await store.get_notice_state())
    assert notice.pending_count == 0
    for item_id in observed["ids"]:
        item = await store.get_message_by_envelope(item_id)
        assert item is not None and item.processing_state is ProcessingState.PROCESSED
    await store.close()


@pytest.mark.asyncio
async def test_runtime_create_reminder_forwards_the_public_timestamp(tmp_path):
    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=ToolReturnAdapter(),
        run_turn=lambda _planned: asyncio.sleep(0),
        workspace=tmp_path,
    )

    reminder = await runtime.create_reminder(
        content="runtime reminder",
        target="channel:sp-1:ch-1",
        intended_at="2026-08-03T02:00:00Z",
    )

    assert reminder["content"] == "runtime reminder"
    assert reminder["intended_at"] == "2026-08-03T02:00:00.000Z"
    await store.close()


@pytest.mark.asyncio
async def test_reconstructed_overdue_reminder_has_one_inbox_event_and_one_turn(tmp_path):
    """Repeated snapshots/restart use the existing reminder Inbox path once."""
    keys = KeyStore(tmp_path / "agent-state" / "keys")
    key = keys.load_or_create_message_backup_dek("agent-a")
    envelope = encrypt_reminder_payload(
        dek=key,
        owner_slug="agent-a",
        reminder_id="reminder-remote",
        occurrence_id="occurrence-remote",
        intended_at_ms=1,
        target="channel:sp-1:ch-1",
        content="reconstructed exact content",
    )
    row = {
        "occurrence_id": "occurrence-remote",
        "revision": 1,
        "reminder_id": "reminder-remote",
        "due_at": "1970-01-01T00:00:00.001Z",
        "lifecycle": "scheduled",
        "lifecycle_at": "1970-01-01T00:00:00.000Z",
        "payload_format": REMINDER_PAYLOAD_FORMAT,
        "opaque_payload": _base64url_encode(envelope),
    }

    class SnapshotTransport:
        keyless = False

        async def get(self, _path):
            return {"occurrences": [row], "next_after": None}

        async def put(self, _path, _body):
            raise AssertionError("delivery is not uploaded by this Inbox test")

        async def post(self, path, body):
            return {
                "occurrence_id": path.split("/")[-2],
                "revision": body["revision"],
                "lifecycle": "scheduled",
                "status": "acquired",
            }

    store = await make_store(tmp_path / "first")
    # The Turn drains the occurrence through ``read_inbox``, which needs a
    # provider that can correlate Inbox tool results.
    adapter = ToolReturnAdapter()
    turns: list[str] = []

    async def run_turn(planned):
        turns.append(planned.provider_input)
        await adapter.admit()
        # Read the notice through the Inbox so the occurrence is genuinely
        # consumed by this Turn instead of staying pending for a redelivery.
        await runtime.read_inbox(limit=50)

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run_turn, workspace=tmp_path,
    )
    sync = ReminderSync(
        store=store,
        keystore=keys,
        owner_slug="agent-a",
        http_client=SnapshotTransport(),
        scheduler=runtime.reminder_scheduler,
    )
    runtime.reminder_scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    assert await sync.reconcile_snapshot() == 1
    assert await sync.reconcile_snapshot() == 0
    assert [item.occurrence_id for item in await runtime.reminder_scheduler.process_due_once()] == [
        "occurrence-remote"
    ]
    assert await runtime.process_once()
    assert not await runtime.process_once()
    assert len(turns) == 1
    await store.close()

    reopened = MessageStore(tmp_path / "first" / "messages.db")
    restarted = GlobalInboxRuntime(
        store=reopened, adapter=Adapter(), run_turn=lambda _planned: None, workspace=tmp_path,
    )
    restarted_sync = ReminderSync(
        store=reopened,
        keystore=keys,
        owner_slug="agent-a",
        http_client=SnapshotTransport(),
        scheduler=restarted.reminder_scheduler,
    )
    restarted.reminder_scheduler.set_delivery_authorizer(
        restarted_sync.authorize_due_delivery
    )
    assert await restarted_sync.reconcile_snapshot() == 0
    assert await restarted.reminder_scheduler.process_due_once() == ()
    event = await reopened.get_message_by_envelope("reminder-occurrence:occurrence-remote")
    assert event is not None
    await reopened.close()


@pytest.mark.asyncio
async def test_different_target_reminder_still_uses_one_global_notice_and_turn(tmp_path):
    store = await make_store(tmp_path)
    ordinary_body = "ordinary body for the first target"
    await receipt(store, "ordinary", 1, content=ordinary_body)
    reminder = await store.create_reminder(
        content="exact content for the second target",
        target="channel:sp-1:ch-2",
        intended_at_ms=1,
    )
    await store.deliver_due_reminders(now_ms=2)

    adapter = ToolReturnAdapter()
    observed: dict[str, object] = {}
    turns = 0

    async def run(planned):
        nonlocal turns
        turns += 1
        assert ordinary_body not in planned.provider_input
        assert "exact content for the second target" not in planned.provider_input
        summary = json.loads(planned.provider_input.split("\n", 2)[1])
        assert summary["message_count"] == 2
        assert summary["targets"] == [
            {"target": "channel:sp-1:ch-1", "count": 1},
            {"target": "channel:sp-1:ch-2", "count": 1},
        ]
        await adapter.admit()
        observed["page"] = await runtime.read_inbox(limit=50)
        observed["ids"] = tuple(runtime.active.message_ids)

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    assert await runtime.process_once()
    assert not await runtime.process_once()
    assert turns == 1
    assert observed["ids"] == (
        "ordinary", f"reminder-occurrence:{reminder.occurrence_id}",
    )
    page = observed["page"]
    assert len(page["messages"]) == 2
    assert ordinary_body in page["messages"][0]
    assert 'event_type="reminder"' in page["messages"][1]
    assert 'target_ref="channel:sp-1:ch-2"' in page["messages"][1]
    assert "exact content for the second target" in page["messages"][1]
    for item_id in observed["ids"]:
        item = await store.get_message_by_envelope(item_id)
        assert item is not None and item.processing_state is ProcessingState.PROCESSED
    await store.close()


@pytest.mark.asyncio
async def test_runtime_owns_reminder_scheduler_start_and_stop(tmp_path):
    store = await make_store(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Scheduler:
        async def run(self):
            started.set()
            await stopped.wait()

        def stop(self):
            stopped.set()

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        reminder_scheduler=Scheduler(),
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    runtime.stop()
    await asyncio.wait_for(task, timeout=1)
    assert stopped.is_set()
    await store.close()


@pytest.mark.asyncio
async def test_runtime_surfaces_owned_reminder_scheduler_failure(tmp_path):
    store = await make_store(tmp_path)

    class FailingScheduler:
        async def run(self):
            raise RuntimeError("timer storage failed")

        def stop(self):
            return None

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        reminder_scheduler=FailingScheduler(),
    )
    with pytest.raises(RuntimeError, match="timer storage failed"):
        await runtime.run()
    await store.close()


@pytest.mark.asyncio
async def test_changed_pending_generation_replaces_accepted_notice_without_losing_unread_rows(
    tmp_path,
):
    store = await make_store(tmp_path)
    first_body = "first-unread-body"
    second_body = "second-unread-body"
    await receipt(store, "first-unread", 10, content=first_body)
    adapter = Adapter()
    notices = []

    async def run(planned):
        notices.append(planned.provider_input)
        await adapter.admit(adapter.session)

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
    )
    assert await runtime.process_once()
    accepted = await store.get_notice_state()
    # Neither Turn admits a read, so each empty Turn re-arms delivery for the
    # unchanged pending set instead of suppressing it.
    assert accepted.is_due_for(adapter.session)

    await receipt(store, "second-unread", 11, content=second_body)
    changed = await store.get_notice_state()
    assert changed.generation == accepted.generation + 1
    assert changed.is_due_for(adapter.session)
    assert await runtime.process_once()
    final = await store.get_notice_state()
    assert final.generation == changed.generation + 1
    assert final.is_due_for(adapter.session)

    assert len(notices) == 2
    assert notices[0] != notices[1]
    assert '"message_count":2' in notices[1]
    assert first_body not in notices[1]
    assert second_body not in notices[1]
    assert [row.envelope_id for row in await store.get_pending()] == [
        "first-unread", "second-unread",
    ]
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["provider_failure", "cancellation"])
async def test_accepted_empty_notice_rearms_after_non_success_terminal(
    tmp_path, terminal,
):
    store = await make_store(tmp_path)
    await receipt(store, f"{terminal}-notice", 10)
    adapter = Adapter()
    admitted = asyncio.Event()

    async def run(_planned):
        await adapter.admit(adapter.session)
        admitted.set()
        if terminal == "provider_failure":
            raise RuntimeError("provider failed after notice acceptance")
        await asyncio.Event().wait()

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
    )
    before = await store.get_notice_state()
    if terminal == "provider_failure":
        assert await runtime.process_once()
    else:
        task = asyncio.create_task(runtime.process_once())
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    rearmed = await store.get_notice_state()
    assert rearmed.generation == before.generation + 1
    assert rearmed.is_due_for(adapter.session)
    assert [row.envelope_id for row in await store.get_pending()] == [
        f"{terminal}-notice",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_crash_recovery_requeues_empty_notice_turn_and_rearms_delivery(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "notice-crash", 11)
    adapter = Adapter()
    seed = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    planned = await seed.plan_pending()
    assert planned is not None
    seed._write_current_turn(planned)
    await store.start_turn(
        turn_id=planned.turn_id,
        provider_session_id=adapter.session,
    )
    before = await store.get_notice_state()
    assert await store.mark_notice_delivered(before.generation, adapter.session)

    recovered = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert not await recovered.recover_current_turn()

    run = await store.get_turn_run(planned.turn_id)
    after = await store.get_notice_state()
    assert run is not None and run.state == "requeued"
    assert [row.envelope_id for row in await store.get_pending()] == [
        "notice-crash"
    ]
    assert after.generation == before.generation + 1
    assert after.last_delivered_generation == before.generation
    assert after.delivery_pending
    assert not recovered.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_startup_recovers_orphaned_turn_without_crash_join(
    tmp_path,
    admitted,
):
    store = await make_store(tmp_path)
    await receipt(store, "orphan-pending", 12)
    notice = await store.get_notice_state()
    assert await store.mark_notice_delivered(notice.generation, "provider-old")
    if admitted:
        await store.admit_messages(
            ["orphan-pending"],
            turn_id="orphan-turn",
            provider_session_id="provider-old",
        )
    else:
        await store.start_turn(
            turn_id="orphan-turn",
            provider_session_id="provider-old",
        )

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    assert await runtime.recover_orphaned_turns() == 1

    run = await store.get_turn_run("orphan-turn")
    repaired = await store.get_notice_state()
    assert run is not None and run.state == "requeued"
    assert [row.envelope_id for row in await store.get_pending()] == [
        "orphan-pending"
    ]
    assert repaired.delivery_pending
    assert await store.get_active_turn_runs() == ()
    assert await runtime.recover_orphaned_turns() == 0
    await store.close()


@pytest.mark.asyncio
async def test_one_turn_reads_two_targets_sends_twice_and_processes_exact_union(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "target-a", 1, channel="ch-a")
    await receipt(store, "target-b", 2, channel="ch-b")

    class MultiReadAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.continuations = {}

        def register_continuation_callback(
            self, callback, planning_cycle_key, **_metadata
        ):
            self.continuations[planning_cycle_key] = callback

        async def admit_key(self, key):
            callback = self.continuations.pop(key)
            await callback(ProviderAdmissionEvent(
                planning_cycle_key=key,
                provider_session_id=self.session,
                provider_turn_id="provider-turn",
                tool_call_id=f"tool-{key}",
                admitted_at=datetime.now(timezone.utc),
            ))

    class Coordinator:
        def __init__(self):
            self.calls = []
            self.provider_session_id = None

        async def send(self, request=None, **kwargs):
            self.calls.append((request, kwargs))
            return {
                "state": "sent",
                "envelope_id": f"sent-{len(self.calls)}",
                "seq": 10 + len(self.calls),
            }

    adapter = MultiReadAdapter()
    coordinator = Coordinator()

    async def run(_planned):
        await adapter.admit()
        for channel in ("ch-a", "ch-b"):
            page = await runtime.read_inbox(
                target=f"channel:sp-1:{channel}",
                limit=10,
                tool_arguments={
                    "target": f"channel:sp-1:{channel}",
                    "limit": 10,
                },
            )
            assert len(page["messages"]) == 1
            key = next(iter(adapter.continuations))
            await adapter.admit_key(key)
            route = runtime.resolve_active_send_route(
                channel, {"destination": channel}, {}
            )
            assert route is not None and route.channel_id == channel
            assert (await runtime.send_delegate.send(
                {"destination": channel, "text": f"reply-{channel}"}
            ))["state"] == "sent"
        assert runtime.resolve_plain_fallback_route() is None
        assert runtime.active.message_ids == ["target-a", "target-b"]

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
        coordinator=coordinator,
    )
    runtime.send_delegate = TrackingSendDelegate(
        coordinator, runtime.attempts, runtime
    )
    assert await runtime.process_once()
    assert [call[0]["destination"] for call in coordinator.calls] == [
        "ch-a", "ch-b"
    ]
    assert [
        (await store.get_message_by_envelope(envelope_id)).processing_state
        for envelope_id in ("target-a", "target-b")
    ] == [ProcessingState.PROCESSED, ProcessingState.PROCESSED]
    await store.close()


@pytest.mark.asyncio
async def test_zero_send_notice_turn_can_succeed_without_output(tmp_path):
    store = await make_store(tmp_path)
    await receipt(store, "zero-send", 1)
    adapter = Adapter()
    sends = []

    async def run(_planned):
        await adapter.admit()
        page = await runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        assert page["messages"]
        # No send is attempted; the Turn still completes its exact union.
        callback = adapter.continuation
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=adapter.continuation_key,
            provider_session_id=adapter.session,
            provider_turn_id="provider-turn",
            tool_call_id="tool-zero",
            admitted_at=datetime.now(timezone.utc),
        ))

    # Add only the continuation seam used by read_inbox to the simple adapter.
    adapter.continuation = None
    adapter.continuation_key = ""

    def register(callback, planning_cycle_key, **_metadata):
        adapter.continuation = callback
        adapter.continuation_key = planning_cycle_key

    adapter.register_continuation_callback = register
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
    )
    assert await runtime.process_once()
    assert sends == []
    assert (
        await store.get_message_by_envelope("zero-send")
    ).processing_state is ProcessingState.PROCESSED
    assert runtime.resolve_plain_fallback_route() is None
    await store.close()


@pytest.mark.asyncio
async def test_notice_replan_rereads_durable_generation_and_aborts_stale_work(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "stale-after-compact", 1)
    adapter = Adapter()
    ran = []

    async def disappear():
        assert await store.quarantine_pending(
            "stale-after-compact", reason="removed during compact refresh"
        )

    controller = ScriptedContext(
        adapter,
        [DecisionOutcome.REPLAN, DecisionOutcome.ADMIT],
        on_replan=disappear,
    )

    async def run(_planned):
        ran.append(True)

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run,
        workspace=tmp_path,
        context_controller=controller,
    )
    assert not await runtime.process_once()
    assert controller.calls == 2
    assert ran == []
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_unrecoverable_notice_pressure_retains_pending_in_degraded_state(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "pressure", 1)
    adapter = Adapter()
    controller = ScriptedContext(adapter, [DecisionOutcome.DEGRADED])
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        context_controller=controller,
    )
    assert not await runtime.process_once()
    assert runtime.health.state == "degraded"
    assert runtime._degraded is True
    assert [row.envelope_id for row in await store.get_pending()] == ["pressure"]
    await store.close()


@pytest.mark.asyncio
async def test_transient_provider_failure_self_recovers_after_backoff_without_ingress(
    tmp_path,
):
    """A transient provider incident must not strand requeued durable work.

    The degrade is a bounded backoff window the runtime ends by itself, so the
    still-pending row is retried without any new message or ``notify()``.
    """
    store = await make_store(tmp_path)
    await receipt(store, "transient", 1)
    adapter = ToolReturnAdapter()
    calls = 0

    async def run(_planned):
        nonlocal calls
        calls += 1
        await adapter.admit()
        if calls == 1:
            raise RuntimeError("provider exhausted")
        await runtime.read_inbox(limit=50)

    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=run, workspace=tmp_path,
    )
    assert await runtime.process_once()
    assert runtime._degraded is True
    assert runtime._degraded_until is not None
    assert [row.envelope_id for row in await store.get_pending()] == ["transient"]

    # Inside the backoff window the runtime must not busy-retry.
    assert not await runtime.process_once()
    assert calls == 1

    # Expire the window only; no ingress, no notify(), no new message.
    runtime._degraded_until = time.monotonic() - 1
    assert await runtime.process_once()
    assert calls == 2
    assert runtime._degraded is False
    assert [row.envelope_id for row in await store.get_pending()] == []
    row = await store.get_message_by_envelope("transient")
    assert row.processing_state == ProcessingState.PROCESSED.value
    await store.close()


@pytest.mark.asyncio
async def test_read_inbox_byte_guard_repaginates_without_lifecycle_mutation(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "guard-1", 1, content="a" * 60_000)
    await receipt(store, "guard-2", 2, content="b" * 60_000)

    class CorrelatingAdapter(Adapter):
        def register_continuation_callback(
            self, callback, planning_cycle_key, **metadata
        ):
            self.continuation = (callback, planning_cycle_key, metadata)

    adapter = CorrelatingAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn-guard"
    runtime.active.provider_session_id = "provider-1"
    page = await runtime.read_inbox(limit=2, tool_arguments={"limit": 2})
    assert len(page["messages"]) == 1
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert page["remaining_count"] == 1
    assert set(page) == {
        "context_version",
        "messages",
        "prior_context",
        "prior_context_has_more",
        "next_cursor",
        "has_more",
        "remaining_count",
        "snapshot_generation",
        "correlation_receipt",
    }
    assert [row.envelope_id for row in await store.get_pending()] == [
        "guard-1", "guard-2"
    ]
    await store.close()


@pytest.mark.asyncio
async def test_read_inbox_admits_exact_page_at_runtime_tool_return(
    tmp_path, caplog,
):
    caplog.set_level(
        logging.INFO,
        logger="puffo_agent.agent.global_inbox_runtime",
    )
    store = await make_store(tmp_path)
    await receipt(store, "tool-return-inbox", 9)
    await store.start_turn(
        turn_id="turn-tool-return",
        provider_session_id="provider-1",
    )
    adapter = ToolReturnAdapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn-tool-return"
    runtime.active.provider_session_id = adapter.session
    runtime.active.provider_turn_id = "provider-turn-tool-return"

    page = await runtime.read_inbox(limit=1, tool_arguments={"limit": 1})

    assert len(page["messages"]) == 1
    assert runtime.active.message_ids == ["tool-return-inbox"]
    assert runtime.active.through_by_channel[("sp-1", "ch-1")] == 9
    stored = await store.get_message_by_envelope("tool-return-inbox")
    assert stored is not None
    assert stored.processing_state is ProcessingState.IN_TURN
    assert runtime.current_turn_path.exists()
    inbox_events = [
        event for event in runtime_events(caplog)
        if event["event"].startswith("inbox.read")
    ]
    assert [event["event"] for event in inbox_events] == [
        "inbox.read_staged", "inbox.read_admitted",
    ]
    assert inbox_events[0]["outcome"] == "tool_return"
    assert inbox_events[1]["provider_turn_id"] == (
        "provider-turn-tool-return"
    )
    await store.close()


def test_plain_fallback_requires_one_unique_admitted_route():
    active = ActiveExactUnion()
    runtime = GlobalInboxRuntime.__new__(GlobalInboxRuntime)
    runtime.active = active
    assert runtime.resolve_plain_fallback_route() is None

    first = MessageRoute("m-1", "channel", "sp", "ch-a")
    duplicate_target = MessageRoute("m-2", "channel", "sp", "ch-a")
    second = MessageRoute("m-3", "channel", "sp", "ch-b")
    active.routes[:] = [first]
    assert runtime.resolve_plain_fallback_route() == first
    active.routes.append(duplicate_target)
    assert runtime.resolve_plain_fallback_route() == first
    active.routes.append(second)
    assert runtime.resolve_plain_fallback_route() is None


class _CrashAfterBoundary(RuntimeError):
    pass


class _CrashCorrelatingAdapter(Adapter):
    def register_continuation_callback(
        self, callback, planning_cycle_key, **_metadata
    ):
        self.continuation = callback
        self.continuation_key = planning_cycle_key

    async def admit_continuation(self, tool_call_id):
        callback, self.continuation = self.continuation, None
        await callback(ProviderAdmissionEvent(
            planning_cycle_key=self.continuation_key,
            provider_session_id=self.session, provider_turn_id="native-turn",
            tool_call_id=tool_call_id, admitted_at=datetime.now(timezone.utc),
        ))


async def _seed_crash_join_turn(tmp_path, message_count):
    store = await make_store(tmp_path)
    for seq in range(1, message_count + 1):
        await receipt(store, f"page-{seq}", seq)
    outbox = RuntimeEventOutbox(tmp_path / "state" / "runtime_events.db")
    outbox.set_active_turn(
        "logical-turn", session_ref="logical-session",
        native_session_id="provider-1",
    )
    adapter = _CrashCorrelatingAdapter()
    seed = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path, agent_id="agent", runtime_event_outbox=outbox,
    )
    await store.start_turn(turn_id="durable-turn", provider_session_id="provider-1")
    seed.active.turn_id = "durable-turn"
    seed.active.provider_session_id = "provider-1"
    seed.active.provider_turn_id = "native-turn"
    cursor, admitted = "", []
    for page_number in range(message_count):
        page = await seed.read_inbox(
            cursor=cursor, limit=1,
            tool_arguments={"cursor": cursor, "limit": 1},
        )
        assert len(page["messages"]) == 1
        await adapter.admit_continuation(f"tool-{page_number + 1}")
        admitted.append(f"page-{page_number + 1}")
        persisted = json.loads(seed.current_turn_path.read_text())
        assert persisted["message_ids"] == admitted
        assert persisted["provider_session_id"] == "provider-1"
        assert persisted["provider_turn_id"] == "native-turn"
        assert persisted["logical_session_ref"] == "logical-session"
        assert persisted["logical_turn_ref"] == "logical-turn"
        cursor = page["next_cursor"]
    return store, outbox, admitted


def _install_crash_join_boundaries(
    monkeypatch, store, outbox, tmp_path, boundary,
):
    requeues = []
    original_requeue = store.requeue_messages

    async def record_requeue(message_ids, *, turn_id):
        terminal_rows = outbox.prefix()
        assert len(terminal_rows) == 1
        assert terminal_rows[0].event["payload"] == {"outcome": "abandoned"}
        requeues.append((tuple(message_ids), turn_id))
        result = await original_requeue(message_ids, turn_id=turn_id)
        if boundary == "requeue" and len(requeues) == 1:
            raise _CrashAfterBoundary("after exact requeue")
        return result

    monkeypatch.setattr(store, "requeue_messages", record_requeue)
    original_enqueue = outbox.enqueue
    enqueue_calls = 0

    async def crash_after_terminal(event, *, terminal=None):
        nonlocal enqueue_calls
        enqueue_calls += 1
        result = await original_enqueue(event, terminal=terminal)
        if boundary == "terminal" and enqueue_calls == 1:
            raise _CrashAfterBoundary("after abandoned persistence")
        return result

    monkeypatch.setattr(outbox, "enqueue", crash_after_terminal)
    adapter = Adapter()
    adapter.session = None
    first = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path, agent_id="agent", runtime_event_outbox=outbox,
    )
    if boundary == "clear":
        original_clear = first._clear_terminal_turn

        def crash_after_clear():
            original_clear()
            raise _CrashAfterBoundary("after active-state clear")

        monkeypatch.setattr(first, "_clear_terminal_turn", crash_after_clear)
    return first, requeues, record_requeue, original_enqueue


async def _assert_crash_join_restart(
    monkeypatch, store, outbox, tmp_path, boundary, admitted,
    requeues, record_requeue, original_enqueue,
):
    monkeypatch.setattr(outbox, "enqueue", original_enqueue)
    if boundary == "requeue":
        monkeypatch.setattr(store, "requeue_messages", record_requeue)
    adapter = Adapter()
    adapter.session = None
    second = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=lambda _planned: None,
        workspace=tmp_path, agent_id="agent", runtime_event_outbox=outbox,
    )
    assert not await second.recover_current_turn()
    rows = outbox.prefix()
    assert len(rows) == 1
    assert rows[0].event["type"] == "turn.finished"
    assert rows[0].event["payload"] == {"outcome": "abandoned"}
    assert rows[0].event_id.startswith("evt_abandoned_")
    assert requeues == [(tuple(admitted), "durable-turn")]
    assert [row.envelope_id for row in await store.get_pending()] == admitted
    assert not second.current_turn_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_count", [1, 2])
@pytest.mark.parametrize("boundary", ["terminal", "requeue", "clear"])
async def test_notice_read_crash_join_restart_is_exact_and_idempotent(
    tmp_path, monkeypatch, message_count, boundary,
):
    store, outbox, admitted = await _seed_crash_join_turn(tmp_path, message_count)
    first, requeues, record_requeue, original_enqueue = _install_crash_join_boundaries(
        monkeypatch, store, outbox, tmp_path, boundary,
    )
    with pytest.raises(_CrashAfterBoundary):
        await first.recover_current_turn()
    await _assert_crash_join_restart(
        monkeypatch, store, outbox, tmp_path, boundary, admitted,
        requeues, record_requeue, original_enqueue,
    )
    outbox.close()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_ids", [[], ["page-1"], ["page-1", "page-1"]])
async def test_crash_join_rejects_empty_partial_or_duplicate_union(
    tmp_path, invalid_ids,
):
    store = await make_store(tmp_path)
    await receipt(store, "page-1", 1)
    await receipt(store, "page-2", 2)
    await store.admit_messages(
        ["page-1", "page-2"],
        turn_id="durable-turn",
        provider_session_id="provider-1",
    )
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    rows = await store.get_in_turn_messages("durable-turn", "provider-1")
    runtime.active.turn_id = "durable-turn"
    runtime.active.provider_session_id = "provider-1"
    runtime._write_current_turn(
        runtime._reconstruct_exact_turn(turn_id="durable-turn", rows=rows)
    )
    raw = json.loads(runtime.current_turn_path.read_text())
    raw["message_ids"] = invalid_ids
    runtime.current_turn_path.write_text(json.dumps(raw))

    assert not await runtime.recover_current_turn()
    assert [row.envelope_id for row in await store.get_pending()] == [
        "page-1", "page-2"
    ]
    assert not runtime.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_crash_join_outbox_session_mismatch_requeues_without_foreign_terminal(
    tmp_path,
):
    store = await make_store(tmp_path)
    await receipt(store, "page-1", 1)
    await store.admit_messages(
        ["page-1"],
        turn_id="durable-turn",
        provider_session_id="provider-1",
    )
    outbox = RuntimeEventOutbox(tmp_path / "state" / "runtime_events.db")
    outbox.set_active_turn(
        "foreign-turn",
        session_ref="foreign-logical-session",
        native_session_id="provider-other",
    )
    adapter = Adapter()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=lambda _planned: None,
        workspace=tmp_path,
        agent_id="agent",
        runtime_event_outbox=outbox,
    )
    rows = await store.get_in_turn_messages("durable-turn", "provider-1")
    runtime.active.turn_id = "durable-turn"
    runtime.active.provider_session_id = "provider-1"
    runtime._write_current_turn(
        runtime._reconstruct_exact_turn(turn_id="durable-turn", rows=rows)
    )
    raw = json.loads(runtime.current_turn_path.read_text())
    raw["logical_session_ref"] = "logical-session"
    raw["logical_turn_ref"] = "logical-turn"
    raw["native_session_id"] = "provider-1"
    runtime.current_turn_path.write_text(json.dumps(raw))

    assert not await runtime.recover_current_turn()
    assert outbox.prefix() == []
    assert outbox.state()["active_turn_ref"] == "foreign-turn"
    assert [row.envelope_id for row in await store.get_pending()] == ["page-1"]
    outbox.close()
    await store.close()


async def _seed_crash_join(store, tmp_path, ids, *, is_encrypted):
    """Write a valid crash join for ``ids`` and return the seeding runtime."""
    for index, envelope_id in enumerate(ids, start=1):
        await receipt(store, envelope_id, index, is_encrypted=is_encrypted)
    await store.admit_messages(
        list(ids),
        turn_id="durable-turn",
        provider_session_id="provider-1",
    )
    seed = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    rows = await store.get_in_turn_messages("durable-turn", "provider-1")
    seed.active.turn_id = "durable-turn"
    seed.active.provider_session_id = "provider-1"
    seed._write_current_turn(
        seed._reconstruct_exact_turn(turn_id="durable-turn", rows=rows)
    )
    return seed


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["complete", "failure", "cancel"])
async def test_crash_resumed_turn_carries_and_clears_encrypted_send_mode(
    tmp_path, terminal,
):
    """A resumed turn derives E2EE facts from its triggering durable rows."""
    from puffo_agent.agent import send_mode
    from puffo_agent.agent.core import AgentAPIError

    store = await make_store(tmp_path)
    await _seed_crash_join(store, tmp_path, ["page-1"], is_encrypted=True)
    observed = []

    class Runner:
        async def __call__(self, _planned):
            raise AssertionError("recovery uses handle_global_inbox_retry")

        async def handle_global_inbox_retry(self, _planned):
            observed.append(send_mode.turn_bundle_encrypted("agent-key"))
            if terminal == "failure":
                raise AgentAPIError("rate limited")
            if terminal == "cancel":
                raise asyncio.CancelledError()
            return None

    recovered = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=Runner(),
        workspace=tmp_path,
        send_mode_keys=("agent-key",),
        max_api_retries=0,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )
    send_mode.clear_turn_bundle(["agent-key"])
    if terminal == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await recovered.recover_current_turn()
    else:
        completed = await recovered.recover_current_turn()
        assert completed is (terminal == "complete")

    # True for the whole resumed turn, and torn down on every terminal path.
    assert observed and all(observed)
    assert send_mode.turn_bundle_encrypted("agent-key") is False
    await store.close()


@pytest.mark.asyncio
async def test_failed_recovery_requeues_grown_membership_not_activation_snapshot(
    tmp_path,
):
    """Rows admitted mid-recovery are requeued with the rest of the turn."""
    from puffo_agent.agent.core import AgentAPIError

    store = await make_store(tmp_path)
    await _seed_crash_join(store, tmp_path, ["page-1"], is_encrypted=True)
    await receipt(store, "page-2", 2)

    class Runner:
        async def __call__(self, _planned):
            raise AssertionError("recovery uses handle_global_inbox_retry")

        async def handle_global_inbox_retry(self, planned):
            run = await store.admit_messages(
                ["page-2"],
                turn_id=planned.turn_id,
                provider_session_id="provider-1",
            )
            recovered.active.message_ids[:] = list(run.message_ids)
            raise AgentAPIError("provider gave up")

    recovered = GlobalInboxRuntime(
        store=store,
        adapter=Adapter(),
        run_turn=Runner(),
        workspace=tmp_path,
        max_api_retries=0,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )
    assert not await recovered.recover_current_turn()

    run = await store.get_turn_run("durable-turn")
    assert run is not None and run.state == "requeued"
    assert sorted(row.envelope_id for row in await store.get_pending()) == [
        "page-1", "page-2",
    ]
    assert not recovered.current_turn_path.exists()
    await store.close()


@pytest.mark.asyncio
async def test_concurrently_held_channels_project_only_their_own_recovery(tmp_path):
    """Held staging is per target, so one channel cannot answer for another.

    ``_send_request`` serializes per ``(space_id, channel_id)``, so two
    channels of one turn can be held and recovered concurrently.  A shared
    staging slot let the later recovery decide what the earlier send reported
    as its own ``synchronized`` / ``recovered_through_seq``.
    """
    store = await make_store(tmp_path)
    await receipt(store, "initial", 1, channel="ch-a")
    await store.admit_messages(
        ["initial"], turn_id="turn", provider_session_id="provider-1",
    )
    await receipt(store, "held-a", 5, channel="ch-a")
    await receipt(store, "held-b", 6, channel="ch-b")
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=ToolReturnAdapter(),
        run_turn=lambda _planned: None,
        workspace=tmp_path,
    )
    runtime.active.turn_id = "turn"
    runtime.active.message_ids[:] = ["initial"]
    runtime.active.provider_session_id = "provider-1"
    runtime.active.routes[:] = [
        MessageRoute("held-a", "channel", "sp-1", "ch-a"),
        MessageRoute("held-b", "channel", "sp-1", "ch-b"),
    ]
    source = runtime.held_recovery_source
    recovered = {"ch-a": asyncio.Event(), "ch-b": asyncio.Event()}
    watermark = {"ch-a": (5, "held-a"), "ch-b": (6, "held-b")}

    class Interleaved:
        held_recovery_source = source

        async def send(self, request=None, **_kwargs):
            channel = str(request["channel"])
            if channel == "ch-b":
                await asyncio.wait_for(recovered["ch-a"].wait(), timeout=1)
            seq, envelope_id = watermark[channel]
            await source.query_held_messages(
                "sp-1", channel, seq, envelope_id, "provider-1",
            )
            recovered[channel].set()
            if channel == "ch-a":
                # ch-b recovers before this held result is staged.
                await asyncio.wait_for(recovered["ch-b"].wait(), timeout=1)
            return {
                "state": "held", "latest_seq": seq,
                "latest_envelope_id": envelope_id,
            }

    delegate = TrackingSendDelegate(Interleaved(), runtime.attempts, runtime)
    first = asyncio.create_task(delegate.send({"channel": "ch-a", "text": "a"}))
    second = await delegate.send({"channel": "ch-b", "text": "b"})
    result = await asyncio.wait_for(first, timeout=1)

    assert (result["latest_seq"], result["recovered_through_seq"]) == (5, 5)
    assert (second["latest_seq"], second["recovered_through_seq"]) == (6, 6)
    assert result["synchronized"] is True and second["synchronized"] is True
    await store.close()
