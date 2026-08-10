"""Prior-context, reminder, and claim lifecycle persistence coverage."""

import asyncio
import sqlite3

import pytest

from puffo_agent.agent.message_store import (
    MessageStore,
    ProcessingState,
    ReceiptDisposition,
)

from test_message_store import (
    _channel_payload,
    _dm_payload,
    _temp_store,
)


@pytest.mark.asyncio
async def test_inbox_page_snapshot_excludes_concurrent_arrival_and_is_read_only(tmp_path):
    store = MessageStore(tmp_path / "pages.db")
    for seq in range(1, 4):
        await store.store_receipt(
            _channel_payload(f"page-{seq}", channel_id="ch_1"),
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="test",
        )
    first = await store.read_inbox_page(limit=2)
    assert [item.envelope_id for item in first.items] == ["page-1", "page-2"]
    assert first.has_more and first.remaining_count == 1
    await store.store_receipt(
        _channel_payload("page-4", channel_id="ch_1"),
        server_seq=4,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    second = await store.read_inbox_page(cursor=first.next_cursor, limit=2)
    assert [item.envelope_id for item in second.items] == ["page-3"]
    assert all(
        item.processing_state is ProcessingState.PENDING
        for item in await store.get_pending()
    )
    with pytest.raises(ValueError, match="another target"):
        await store.read_inbox_page(
            target="channel:sp_1:ch_1",
            cursor=first.next_cursor,
            limit=2,
        )
    await store.close()


async def _seed_prior_context(store):
    await store.store_receipt(
        _channel_payload("prior-human", channel_id="ch_context", content="human"),
        server_seq=1,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.admit_messages(
        ["prior-human"], turn_id="prior-human-turn", provider_session_id="provider"
    )
    await store.mark_processed(["prior-human"], turn_id="prior-human-turn")
    await store.store_receipt(
        _channel_payload(
            "prior-self-echo",
            channel_id="ch_context",
            sender_slug="agent",
            content="agent contribution",
        ),
        server_seq=2,
        disposition=ReceiptDisposition.TERMINAL,
        reason="self echo",
    )
    await store.store_receipt(
        _channel_payload(
            "prior-in-turn",
            channel_id="ch_context",
            content="still in turn",
        ),
        server_seq=3,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.admit_messages(
        ["prior-in-turn"], turn_id="prior-in-turn-turn", provider_session_id="provider"
    )
    await store.store_receipt(
        _channel_payload(
            "prior-page",
            channel_id="ch_context",
            content="newly admitted page",
        ),
        server_seq=4,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.store_receipt(
        _channel_payload(
            "prior-future",
            channel_id="ch_context",
            content="future pending work",
        ),
        server_seq=5,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.store_receipt(
        _channel_payload(
            "prior-other-channel",
            channel_id="ch_other",
            content="other channel",
        ),
        server_seq=6,
        disposition=ReceiptDisposition.TERMINAL,
        reason="test",
    )
    await store.store_receipt(
        _channel_payload(
            "prior-other-thread",
            channel_id="ch_context",
            thread_root_id="other-root",
            content="other thread",
        ),
        server_seq=7,
        disposition=ReceiptDisposition.TERMINAL,
        reason="test",
    )

    return await store.get_message_by_envelope("prior-page")


async def _assert_prior_context_pages(store, anchor):
    assert anchor is not None
    context = await store.get_prior_context(anchor)
    assert [item.envelope_id for item in context] == [
        "prior-human",
        "prior-self-echo",
    ]
    assert all(item.server_seq < anchor.server_seq for item in context)
    assert all(
        item.processing_state is ProcessingState.PROCESSED
        or item.receipt_disposition is ReceiptDisposition.TERMINAL
        for item in context
    )
    complete_page = await store.get_prior_context_page(anchor)
    assert complete_page.items == context
    assert complete_page.has_more is False

    limited = await store.get_prior_context(anchor, limit=1)
    assert [item.envelope_id for item in limited] == ["prior-self-echo"]
    limited_page = await store.get_prior_context_page(anchor, limit=1)
    assert limited_page.items == limited
    assert limited_page.has_more is True
    bounded = await store.get_prior_context(
        anchor, limit=20, max_bytes=len("agent contribution")
    )
    assert [item.envelope_id for item in bounded] == ["prior-self-echo"]
    assert sum(len(str(item.content).encode("utf-8")) for item in bounded) <= len(
        "agent contribution"
    )
    bounded_page = await store.get_prior_context_page(
        anchor, limit=20, max_bytes=len("agent contribution")
    )
    assert bounded_page.items == bounded
    assert bounded_page.has_more is True

    return bounded_page


async def _assert_prior_context_lifecycles(store):
    in_turn = await store.get_message_by_envelope("prior-in-turn")
    future = await store.get_message_by_envelope("prior-future")
    assert in_turn is not None and in_turn.processing_state is ProcessingState.IN_TURN
    assert future is not None and future.processing_state is ProcessingState.PENDING
    await store.requeue_messages(
        ["prior-in-turn"], turn_id="prior-in-turn-turn"
    )


@pytest.mark.asyncio
async def test_prior_context_is_bounded_ordered_and_lifecycle_filtered(tmp_path):
    store = MessageStore(tmp_path / "prior-context.db")
    anchor = await _seed_prior_context(store)
    await _assert_prior_context_pages(store, anchor)
    await _assert_prior_context_lifecycles(store)
    await store.close()


@pytest.mark.asyncio
async def test_prior_context_dm_route_excludes_other_peers_and_future_rows(tmp_path):
    store = MessageStore(tmp_path / "prior-context-dm.db")
    await store.store_receipt(
        _dm_payload(
            "dm-prior", "peer-1", "agent", content="earlier DM"
        ),
        server_seq=1,
        disposition=ReceiptDisposition.TERMINAL,
        reason="test",
    )
    await store.store_receipt(
        _dm_payload(
            "dm-other", "peer-2", "agent", content="other DM"
        ),
        server_seq=2,
        disposition=ReceiptDisposition.TERMINAL,
        reason="test",
    )
    await store.store_receipt(
        _dm_payload(
            "dm-page", "peer-1", "agent", content="current DM"
        ),
        server_seq=3,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.store_receipt(
        _dm_payload(
            "dm-future", "peer-1", "agent", content="future DM"
        ),
        server_seq=4,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )

    anchor = await store.get_message_by_envelope("dm-page")
    assert anchor is not None
    context = await store.get_prior_context(anchor)
    assert [item.envelope_id for item in context] == ["dm-prior"]
    assert all(item.envelope_kind == "dm" for item in context)
    assert all(item.sender_slug == "peer-1" or item.recipient_slug == "peer-1" for item in context)
    assert [item.envelope_id for item in await store.get_pending()] == [
        "dm-page",
        "dm-future",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_inbox_page_traverses_more_than_fifty_with_complete_metadata(tmp_path):
    store = MessageStore(tmp_path / "deep-pages.db")
    for seq in range(1, 74):
        await store.store_receipt(
            _channel_payload(f"deep-{seq:03d}", channel_id="ch_deep"),
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="test",
        )

    ids: list[str] = []
    cursor = ""
    generation = None
    remaining = []
    while True:
        page = await store.read_inbox_page(
            target="channel:sp_1:ch_deep",
            cursor=cursor,
            limit=17,
        )
        ids.extend(item.envelope_id for item in page.items)
        generation = page.snapshot_generation if generation is None else generation
        assert page.snapshot_generation == generation
        assert isinstance(page.next_cursor, str)
        assert isinstance(page.has_more, bool)
        assert isinstance(page.remaining_count, int)
        remaining.append(page.remaining_count)
        if not page.has_more:
            assert page.next_cursor == ""
            break
        assert page.next_cursor
        cursor = page.next_cursor

    assert ids == [f"deep-{seq:03d}" for seq in range(1, 74)]
    assert remaining == [56, 39, 22, 5, 0]
    assert len(await store.get_pending()) == 73
    await store.close()


@pytest.mark.asyncio
async def test_reminder_schema_migrates_additively_without_changing_existing_inbox(tmp_path):
    import sqlite3

    path = tmp_path / "pre-reminder.db"
    store = MessageStore(path, now_ms=lambda: 1_000)
    await store.store_receipt(
        _channel_payload("server-before", sent_at=1),
        server_seq=1,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await store.store_local_event(
        _channel_payload("local-before", sent_at=2), reason="test",
    )
    await store.start_turn(turn_id="empty-turn", provider_session_id="provider")
    notice_before = await store.get_notice_state()
    await store.close()

    # Simulate the exact pre-slice file: all existing Inbox tables/data stay,
    # only the additive reminder table is absent.
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE reminder_occurrences")
    connection.commit()
    connection.close()

    reopened = MessageStore(path, now_ms=lambda: 2_000)
    await reopened.open()
    db = await reopened._ensure_db()
    async with db.execute("PRAGMA table_info(reminder_occurrences)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    assert columns == {
        "reminder_id", "occurrence_id", "target", "content", "intended_at_ms",
        "state", "created_at_ms", "claimed_at_ms", "actual_fire_at_ms",
        "cancelled_at_ms", "delivered_at_ms", "delivered_event_id",
        "revision", "server_ack_revision", "payload_format", "opaque_payload",
        "sync_retry_after_ms", "sync_retry_count", "sync_permanent_revision",
        "sync_permanent_code", "delivery_claim_id", "delivery_claim_acquired",
    }
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'reminder_occurrences'"
    ) as cursor:
        schema = (await cursor.fetchone())["sql"]
    assert "'scheduled','claimed','cancelled','delivered'" in schema.replace(" ", "")
    assert [item.envelope_id for item in await reopened.get_pending()] == [
        "server-before", "local-before",
    ]
    assert await reopened.get_notice_state() == notice_before
    run = await reopened.get_turn_run("empty-turn")
    assert run is not None and run.state == ProcessingState.IN_TURN.value
    await reopened.close()


@pytest.mark.asyncio
async def test_reminder_sync_schema_backfills_old_lifecycles_without_losing_facts(tmp_path):
    """The remote outbox is additive to the accepted local reminder table."""
    path = tmp_path / "old-reminders.db"
    bootstrap = MessageStore(path, now_ms=lambda: 1_000)
    await bootstrap.store_receipt(
        _channel_payload("existing-inbox", sent_at=1),
        server_seq=1,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await bootstrap.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE reminder_occurrences")
    connection.execute(
        """CREATE TABLE reminder_occurrences (
            reminder_id TEXT PRIMARY KEY,
            occurrence_id TEXT NOT NULL UNIQUE,
            target TEXT NOT NULL,
            content TEXT NOT NULL,
            intended_at_ms INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            claimed_at_ms INTEGER,
            actual_fire_at_ms INTEGER,
            cancelled_at_ms INTEGER,
            delivered_at_ms INTEGER,
            delivered_event_id TEXT UNIQUE
        )"""
    )
    rows = [
        ("scheduled", "occ-scheduled", "scheduled", None, None, None, None),
        ("claimed", "occ-claimed", "claimed", 20, 20, None, None),
        ("cancelled", "occ-cancelled", "cancelled", None, None, 30, None),
        ("delivered", "occ-delivered", "delivered", 40, 40, None, 50),
    ]
    for reminder_id, occurrence_id, state, claimed_at, fire_at, cancelled_at, delivered_at in rows:
        connection.execute(
            """INSERT INTO reminder_occurrences
               (reminder_id, occurrence_id, target, content, intended_at_ms,
                state, created_at_ms, claimed_at_ms, actual_fire_at_ms,
                cancelled_at_ms, delivered_at_ms, delivered_event_id)
               VALUES (?, ?, 'dm:peer', 'old exact content', 10, ?, 1, ?, ?, ?, ?, ?)""",
            (
                reminder_id, occurrence_id, state, claimed_at, fire_at,
                cancelled_at, delivered_at,
                f"reminder-occurrence:{occurrence_id}" if delivered_at else None,
            ),
        )
    connection.commit()
    connection.close()

    store = MessageStore(path, now_ms=lambda: 1_000)
    await store.open()
    db = await store._ensure_db()
    async with db.execute(
        """SELECT occurrence_id, state, revision, server_ack_revision,
                  payload_format, opaque_payload, sync_retry_count,
                  sync_permanent_code
           FROM reminder_occurrences ORDER BY occurrence_id"""
    ) as cursor:
        migrated = {row["occurrence_id"]: dict(row) for row in await cursor.fetchall()}
    assert migrated["occ-scheduled"]["revision"] == 1
    assert migrated["occ-claimed"]["revision"] == 1
    assert migrated["occ-cancelled"]["revision"] == 2
    assert migrated["occ-delivered"]["revision"] == 2
    assert all(row["server_ack_revision"] == 0 for row in migrated.values())
    assert all(row["payload_format"] is None for row in migrated.values())
    assert [item.envelope_id for item in await store.get_pending()] == ["existing-inbox"]
    assert (await store.get_reminder("delivered")).delivered_event_id == (
        "reminder-occurrence:occ-delivered"
    )
    await store.close()


@pytest.mark.asyncio
async def test_reminder_sync_revisions_and_acknowledgments_are_transactional(tmp_path):
    store = MessageStore(tmp_path / "messages.db", now_ms=lambda: 2_000)
    created = await store.create_reminder(
        reminder_id="reminder-state", occurrence_id="occurrence-state",
        target="dm:peer", content="exact private", intended_at_ms=1_000,
        created_at_ms=500,
    )
    record = await store.get_reminder_sync_record(created.occurrence_id)
    assert record is not None and (record.state, record.revision, record.server_ack_revision) == (
        "scheduled", 1, 0,
    )
    await store.claim_due_reminders(now_ms=2_000)
    record = await store.get_reminder_sync_record(created.occurrence_id)
    assert record is not None and (record.state, record.revision) == ("claimed", 1)

    await store.schedule_reminder_sync_retry(
        occurrence_id=created.occurrence_id, revision=1, retry_after_ms=9_000,
    )
    cancelled = await store.cancel_reminder(created.reminder_id, cancelled_at_ms=2_000)
    assert cancelled.state == "cancelled"
    record = await store.get_reminder_sync_record(created.occurrence_id)
    assert record is not None and (record.revision, record.sync_retry_count) == (2, 0)
    assert not await store.acknowledge_reminder_sync_revision(
        occurrence_id=created.occurrence_id, revision=1,
    )
    assert await store.acknowledge_reminder_sync_revision(
        occurrence_id=created.occurrence_id, revision=2,
    )
    await store.cancel_reminder(created.reminder_id, cancelled_at_ms=2_100)
    record = await store.get_reminder_sync_record(created.occurrence_id)
    assert record is not None and (record.revision, record.server_ack_revision) == (2, 2)

    delivered = await store.create_reminder(
        reminder_id="reminder-delivered", occurrence_id="occurrence-delivered",
        target="dm:peer", content="deliver exactly once", intended_at_ms=1_000,
        created_at_ms=500,
    )
    await store.deliver_due_reminders(now_ms=2_000)
    delivered_record = await store.get_reminder_sync_record(delivered.occurrence_id)
    assert delivered_record is not None
    assert (delivered_record.state, delivered_record.revision) == ("delivered", 2)
    assert [item.envelope_id for item in await store.get_pending()] == [
        f"reminder-occurrence:{delivered.occurrence_id}"
    ]
    await store.close()


@pytest.mark.asyncio
async def test_reminder_identity_projection_order_and_reopen_are_stable(tmp_path):
    now = [1_000]
    path = tmp_path / "reminders.db"
    store = MessageStore(path, now_ms=lambda: now[0])
    later = await store.create_reminder(
        content="later exact text",
        target="channel:sp_1:ch_1:thread:root_1",
        intended_at_ms=5_000,
    )
    earlier = await store.create_reminder(
        content="earlier exact text",
        target="dm:peer_1",
        intended_at_ms=2_000,
    )
    assert later.reminder_id and later.occurrence_id
    assert later.reminder_id != later.occurrence_id
    assert earlier.reminder_id != earlier.occurrence_id
    listed = await store.list_reminders(limit=50)
    assert [item.reminder_id for item in listed] == [
        earlier.reminder_id, later.reminder_id,
    ]
    assert later.as_dict() == {
        "reminder_id": later.reminder_id,
        "occurrence_id": later.occurrence_id,
        "state": "scheduled",
        "target": "channel:sp_1:ch_1:thread:root_1",
        "content": "later exact text",
        "intended_at": "1970-01-01T00:00:05.000Z",
        "actual_fire_at": None,
        "created_at": "1970-01-01T00:00:01.000Z",
        "cancelled_at": None,
        "delivered_at": None,
    }
    await store.close()

    reopened = MessageStore(path, now_ms=lambda: now[0])
    restored = await reopened.get_reminder(later.reminder_id)
    assert restored == later
    await reopened.close()


@pytest.mark.asyncio
async def test_reminder_restart_boundaries_and_cancellation_are_atomic(tmp_path, monkeypatch):
    now = [1_000]
    path = tmp_path / "reminder-boundaries.db"
    store = MessageStore(path, now_ms=lambda: now[0])
    scheduled = await store.create_reminder(
        content="scheduled", target="channel:sp:ch", intended_at_ms=2_000,
    )
    claimed = await store.create_reminder(
        content="claimed", target="channel:sp:ch", intended_at_ms=1_000,
    )
    assert [item.reminder_id for item in await store.claim_due_reminders(now_ms=1_000)] == [
        claimed.reminder_id
    ]
    await store.close()

    reopened = MessageStore(path, now_ms=lambda: 3_000)
    # A scheduled and a previously claimed occurrence both recover to one
    # durable event, never a second intent/occurrence.
    recovered = await reopened.deliver_due_reminders(now_ms=3_000)
    assert {item.reminder_id for item in recovered} == {
        scheduled.reminder_id, claimed.reminder_id,
    }
    assert len(await reopened.get_pending()) == 2

    rollback = await reopened.create_reminder(
        content="rollback", target="channel:sp:ch", intended_at_ms=1,
    )
    original_insert = reopened._insert_local_event_in_transaction

    async def fail_insert(*_args, **_kwargs):
        raise RuntimeError("injected reminder delivery rollback")

    monkeypatch.setattr(reopened, "_insert_local_event_in_transaction", fail_insert)
    with pytest.raises(RuntimeError, match="rollback"):
        await reopened.deliver_due_reminders(now_ms=3_000)
    incomplete = await reopened.get_reminder(rollback.reminder_id)
    assert incomplete is not None and incomplete.state == "claimed"
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{rollback.occurrence_id}"
    ) is None
    monkeypatch.setattr(reopened, "_insert_local_event_in_transaction", original_insert)
    await reopened.close()

    # The interrupted transaction leaves a recoverable claimed fact on disk,
    # not a partial Inbox event. Recovery after a real reopen owns delivery.
    reopened = MessageStore(path, now_ms=lambda: 3_001)
    incomplete = await reopened.get_reminder(rollback.reminder_id)
    assert incomplete is not None and incomplete.state == "claimed"
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{rollback.occurrence_id}"
    ) is None
    assert [item.reminder_id for item in await reopened.deliver_due_reminders(now_ms=3_001)] == [
        rollback.reminder_id
    ]

    cancelled = await reopened.create_reminder(
        content="cancel", target="channel:sp:ch", intended_at_ms=4_000,
    )
    first_cancel = await reopened.cancel_reminder(cancelled.reminder_id, cancelled_at_ms=3_100)
    second_cancel = await reopened.cancel_reminder(cancelled.reminder_id, cancelled_at_ms=3_200)
    assert first_cancel.state == second_cancel.state == "cancelled"
    assert second_cancel.cancelled_at_ms == 3_100
    assert not await reopened.deliver_due_reminders(now_ms=5_000)
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{cancelled.occurrence_id}"
    ) is None

    claimed_cancel = await reopened.create_reminder(
        content="claimed cancel", target="channel:sp:ch", intended_at_ms=1,
    )
    await reopened.claim_due_reminders(now_ms=3_500)
    claimed_cancelled = await reopened.cancel_reminder(claimed_cancel.reminder_id)
    assert claimed_cancelled.state == "cancelled"
    assert not await reopened.deliver_due_reminders(now_ms=5_000)
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{claimed_cancel.occurrence_id}"
    ) is None

    delivered = await reopened.create_reminder(
        content="history stays", target="channel:sp:ch", intended_at_ms=1,
    )
    await reopened.deliver_due_reminders(now_ms=5_000)
    after_delivery_cancel = await reopened.cancel_reminder(delivered.reminder_id)
    assert after_delivery_cancel.state == "delivered"
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{delivered.occurrence_id}"
    ) is not None
    await reopened.close()


@pytest.mark.asyncio
async def test_claimed_cancel_delivery_race_serializes_to_one_valid_terminal_state():
    store = _temp_store()
    reminder = await store.create_reminder(
        content="race", target="channel:sp:ch", intended_at_ms=1,
    )
    await store.claim_due_reminders(now_ms=1)
    cancel, delivered = await asyncio.gather(
        store.cancel_reminder(reminder.reminder_id, cancelled_at_ms=2),
        store.deliver_due_reminders(now_ms=2),
    )
    terminal = await store.get_reminder(reminder.reminder_id)
    assert terminal is not None
    event = await store.get_message_by_envelope(
        f"reminder-occurrence:{reminder.occurrence_id}"
    )
    if terminal.state == "cancelled":
        assert event is None and delivered == () and cancel.state == "cancelled"
    else:
        assert terminal.state == "delivered" and event is not None
        assert cancel.state == "delivered"
    await store.close()


@pytest.mark.asyncio
async def test_stale_authorization_cannot_cross_an_envelope_fence_between_connections(
    tmp_path, monkeypatch,
):
    """A second runtime may persist the fence after local authorization."""
    now = 2_000
    path = tmp_path / "reminder-envelope-fence.db"
    first = MessageStore(path, now_ms=lambda: now)
    second = MessageStore(path, now_ms=lambda: now)

    async def create_due(name: str):
        return await first.create_reminder(
            reminder_id=f"reminder-{name}",
            occurrence_id=f"occurrence-{name}",
            content="due",
            target="dm:peer",
            intended_at_ms=1_000,
            created_at_ms=500,
        )

    async def persist_fence(occurrence_id: str) -> None:
        await second.persist_reminder_envelope(
            occurrence_id=occurrence_id,
            payload_format="puffo-reminder-aead-v1",
            opaque_payload=f"fence:{occurrence_id}".encode("ascii"),
        )

    blocked_before_claim = await create_due("before-claim")
    await persist_fence(blocked_before_claim.occurrence_id)
    assert await first.claim_authorized_reminders(
        (blocked_before_claim.occurrence_id,), now_ms=now,
    ) == ()
    assert (await first.get_reminder(blocked_before_claim.reminder_id)).state == "scheduled"

    blocked_before_delivery = await create_due("before-delivery")
    assert [item.occurrence_id for item in await first.claim_authorized_reminders(
        (blocked_before_delivery.occurrence_id,), now_ms=now,
    )] == [blocked_before_delivery.occurrence_id]
    await persist_fence(blocked_before_delivery.occurrence_id)
    assert await first.deliver_authorized_reminders(
        (blocked_before_delivery.occurrence_id,), now_ms=now,
    ) == ()
    assert (await first.get_reminder(blocked_before_delivery.reminder_id)).state == "claimed"

    blocked_during_delivery = await create_due("during-delivery")
    assert [item.occurrence_id for item in await first.claim_authorized_reminders(
        (blocked_during_delivery.occurrence_id,), now_ms=now,
    )] == [blocked_during_delivery.occurrence_id]
    original_deliver = first._deliver_claimed_reminder_unlocked

    async def persist_before_final_delivery(reminder_id: str, *, now_ms: int):
        assert reminder_id == blocked_during_delivery.reminder_id
        await persist_fence(blocked_during_delivery.occurrence_id)
        return await original_deliver(reminder_id, now_ms=now_ms)

    monkeypatch.setattr(
        first, "_deliver_claimed_reminder_unlocked", persist_before_final_delivery,
    )
    assert await first.deliver_authorized_reminders(
        (blocked_during_delivery.occurrence_id,), now_ms=now,
    ) == ()
    assert (await first.get_reminder(blocked_during_delivery.reminder_id)).state == "claimed"
    for reminder in (
        blocked_before_claim,
        blocked_before_delivery,
        blocked_during_delivery,
    ):
        assert await first.get_message_by_envelope(
            f"reminder-occurrence:{reminder.occurrence_id}"
        ) is None
    await first.close()
    await second.close()
