from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.reminder_scheduler import (
    ReminderDeliveryAuthorization,
    ReminderScheduler,
)


@pytest.mark.asyncio
async def test_scheduler_delivers_due_and_late_facts_without_early_fire(tmp_path):
    now = [1_000]
    store = MessageStore(tmp_path / "messages.db", now_ms=lambda: now[0])
    wakes: list[str] = []
    scheduler = ReminderScheduler(
        store=store, notify=lambda: wakes.append("wake"), now_ms=lambda: now[0],
    )
    future = await scheduler.create_reminder(
        content="future exact content",
        target="channel:sp:ch",
        intended_at="1970-01-01T00:00:02Z",
    )
    assert await scheduler.process_due_once() == ()
    assert (await store.get_reminder(future["reminder_id"])).state == "scheduled"
    assert wakes == []

    late = await scheduler.create_reminder(
        content="past exact content",
        target="dm:peer",
        intended_at="1970-01-01T00:00:00.500Z",
    )
    delivered = await scheduler.process_due_once()
    assert [item.reminder_id for item in delivered] == [late["reminder_id"]]
    late_fact = delivered[0].as_dict()
    assert late_fact["actual_fire_at"] >= late_fact["intended_at"]
    assert wakes == ["wake"]

    now[0] = 2_000
    delivered = await scheduler.process_due_once()
    assert [item.reminder_id for item in delivered] == [future["reminder_id"]]
    await store.close()


@pytest.mark.asyncio
async def test_scheduler_recovers_claimed_work_after_reopen(tmp_path):
    now = [5_000]
    path = tmp_path / "claimed.db"
    store = MessageStore(path, now_ms=lambda: now[0])
    reminder = await store.create_reminder(
        content="recover", target="channel:sp:ch", intended_at_ms=1_000,
    )
    await store.claim_due_reminders(now_ms=5_000)
    await store.close()

    reopened = MessageStore(path, now_ms=lambda: now[0])
    wakes: list[str] = []
    scheduler = ReminderScheduler(
        store=reopened, notify=lambda: wakes.append("wake"), now_ms=lambda: now[0],
    )
    listed = await scheduler.list_reminders(state="scheduled")
    assert listed["reminders"][0]["state"] == "scheduled"
    assert listed["reminders"][0]["actual_fire_at"] is None
    with pytest.raises(ValueError, match="scheduled, cancelled, delivered"):
        await scheduler.list_reminders(state="claimed")
    delivered = await scheduler.process_due_once()
    assert [item.reminder_id for item in delivered] == [reminder.reminder_id]
    assert (await reopened.get_reminder(reminder.reminder_id)).state == "delivered"
    assert wakes == ["wake"]
    await reopened.close()


@pytest.mark.asyncio
async def test_scheduler_without_authorizer_ignores_remote_prepared_work(tmp_path):
    store = MessageStore(tmp_path / "remote.db", now_ms=lambda: 2_000)
    reminder = await store.create_reminder(
        content="remote", target="dm:peer", intended_at_ms=1_000,
    )
    await store.persist_reminder_envelope(
        occurrence_id=reminder.occurrence_id,
        payload_format="opaque-v1",
        opaque_payload=b"encrypted",
    )
    scheduler = ReminderScheduler(
        store=store, notify=lambda: None, now_ms=lambda: 2_000,
    )

    assert await scheduler.process_due_once() == ()
    assert (await store.get_reminder(reminder.reminder_id)).state == "scheduled"
    assert await store.next_reminder_deadline(now_ms=2_000, local_only=True) is None
    await store.close()


@pytest.mark.asyncio
async def test_scheduler_notifies_every_lifecycle_commit_once(tmp_path):
    store = MessageStore(tmp_path / "batch.db", now_ms=lambda: 2_000)
    commits: list[str] = []
    scheduler = ReminderScheduler(
        store=store,
        notify=lambda: None,
        now_ms=lambda: 2_000,
        on_lifecycle_committed=lambda: commits.append("committed"),
    )
    for content in ("reminder one", "reminder two"):
        await scheduler.create_reminder(
            content=content,
            target="dm:peer",
            intended_at="1970-01-01T00:00:01Z",
        )

    assert commits == ["committed", "committed"]
    assert len(await scheduler.process_due_once()) == 2
    assert commits == ["committed", "committed", "committed"]
    assert await scheduler.process_due_once() == ()
    assert commits == ["committed", "committed", "committed"]

    future = await scheduler.create_reminder(
        content="cancel", target="dm:peer", intended_at="1970-01-01T00:00:03Z",
    )
    assert len(commits) == 4
    await scheduler.cancel_reminder(reminder_id=str(future["reminder_id"]))
    assert len(commits) == 5
    await store.close()


@pytest.mark.asyncio
async def test_create_rejects_content_that_cannot_enter_remote_envelope(tmp_path):
    store = MessageStore(tmp_path / "oversized.db")
    scheduler = ReminderScheduler(store=store, notify=lambda: None)

    with pytest.raises(ValueError, match="remote envelope limit"):
        await scheduler.create_reminder(
            content="x" * 40_000,
            target="dm:peer",
            intended_at="2030-01-01T00:00:00Z",
        )

    assert await store.list_reminders() == ()
    await store.close()


@pytest.mark.asyncio
async def test_scheduler_earlier_create_replans_wait_and_stop_has_no_leaked_task(tmp_path):
    now = [1_000]
    waits: list[float | None] = []
    entered_wait = asyncio.Event()

    async def wait_for_change(event: asyncio.Event, timeout: float | None) -> None:
        waits.append(timeout)
        entered_wait.set()
        await event.wait()

    store = MessageStore(tmp_path / "wait.db", now_ms=lambda: now[0])
    scheduler = ReminderScheduler(
        store=store,
        notify=lambda: None,
        now_ms=lambda: now[0],
        wait_for_change=wait_for_change,
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(entered_wait.wait(), timeout=1)
    assert waits == [None]

    async def wait_for_count(count: int) -> None:
        while len(waits) < count:
            await asyncio.sleep(0)

    await scheduler.create_reminder(
        content="later", target="channel:sp:ch",
        intended_at="1970-01-01T00:00:10Z",
    )
    await asyncio.wait_for(wait_for_count(2), timeout=1)
    assert waits[-1] == 9.0

    await scheduler.create_reminder(
        content="earlier", target="channel:sp:ch",
        intended_at="1970-01-01T00:00:02Z",
    )
    await asyncio.wait_for(wait_for_count(3), timeout=1)
    assert waits[-1] == 1.0
    scheduler.stop()
    await task
    assert task.done() and not task.cancelled()
    await store.close()


@pytest.mark.asyncio
async def test_blocked_delivery_claim_never_postpones_a_nearer_reminder(tmp_path):
    """A blocked claim owns only its own retry, not everyone else's deadline.

    The blocked occurrence stays ``scheduled`` with a past ``intended_at_ms``,
    so the durable "next deadline" answer is that past row.  Folding the claim
    retry over it must not hide an independent occurrence that is due before
    the retry, while a blocked-only run must still wait out the retry instead
    of hot-spinning on the past deadline.
    """
    now = [1_000]
    waits: list[float | None] = []
    entered = asyncio.Event()

    async def wait_for_change(event: asyncio.Event, timeout: float | None) -> None:
        waits.append(timeout)
        entered.set()
        await event.wait()

    store = MessageStore(tmp_path / "blocked.db", now_ms=lambda: now[0])
    blocked = await store.create_reminder(
        content="due but blocked", target="channel:sp:ch", intended_at_ms=500,
    )
    nearer = await store.create_reminder(
        content="independent", target="channel:sp:ch", intended_at_ms=3_000,
    )

    async def authorizer(_candidates):
        return ReminderDeliveryAuthorization(retry_after_ms=now[0] + 5_000)

    scheduler = ReminderScheduler(
        store=store,
        notify=lambda: None,
        now_ms=lambda: now[0],
        wait_for_change=wait_for_change,
        delivery_authorizer=authorizer,
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert waits == [pytest.approx(2.0)]
    assert (await store.get_reminder(blocked.reminder_id)).state == "scheduled"

    await store.cancel_reminder(nearer.reminder_id)
    entered.clear()
    scheduler.signal()
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert waits[-1] == pytest.approx(5.0)

    scheduler.stop()
    await task
    await store.close()
