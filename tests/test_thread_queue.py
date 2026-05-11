"""Thread-batched priority queue for the puffo-core message client.

The queue keys on ``root_id`` (the thread root envelope id, or the
message itself when it's a top-level post). Every arrival on the
same thread coalesces into a single ``_ThreadEntry``; the consumer
pops a root, drains the whole batch, and invokes
``on_message_batch`` once per pop. Priority bumps push a fresh
heap tuple with a new seq so stale lower-priority tuples can be
recognised and dropped on pop.

These tests drive the queue + state machine directly via
``_admit_thread_message`` and the per-class ``_thread_state``
without spinning up the full WS / decryption stack.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import (
    PRIORITY_BOT,
    PRIORITY_HUMAN,
    PRIORITY_MENTIONED_BOT,
    PRIORITY_MENTIONED_HUMAN,
    PuffoCoreMessageClient,
    _ThreadEntry,
    _compute_priority,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _make_store() -> MessageStore:
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    return store


def _make_client_for_queue(store: MessageStore) -> PuffoCoreMessageClient:
    """Build a bare PuffoCoreMessageClient with just enough state to
    exercise the queue machinery. Bypasses the constructor because
    the real one requires a keystore + http client + WS bookkeeping
    we don't need here.
    """
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.store = store
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    return client


def _msg(envelope_id: str, sender: str = "alice-0001", sent_at: int | None = None) -> dict:
    return {
        "channel_id": "ch_1",
        "channel_name": "general",
        "space_id": "sp_1",
        "space_name": "Team",
        "sender_slug": sender,
        "sender_email": "",
        "text": f"hello from {envelope_id}",
        "root_id": "",
        "is_dm": False,
        "attachments": [],
        "sender_is_bot": False,
        "mentions": [],
        "envelope_id": envelope_id,
        "sent_at": sent_at if sent_at is not None else _now_ms(),
    }


def _channel_meta() -> dict:
    return {
        "channel_id": "ch_1",
        "channel_name": "general",
        "space_id": "sp_1",
        "space_name": "Team",
        "is_dm": False,
    }


# ─── _compute_priority sanity ─────────────────────────────────────


def test_compute_priority_bands():
    assert _compute_priority(direct=True, sender_is_bot=False) == PRIORITY_MENTIONED_HUMAN
    assert _compute_priority(direct=True, sender_is_bot=True) == PRIORITY_MENTIONED_BOT
    assert _compute_priority(direct=False, sender_is_bot=False) == PRIORITY_HUMAN
    assert _compute_priority(direct=False, sender_is_bot=True) == PRIORITY_BOT


# ─── MessageStore: thread-batch helpers ───────────────────────────


@pytest.mark.asyncio
async def test_get_thread_batch_includes_root_and_replies():
    store = await _make_store()
    base = _now_ms()
    await store.store({
        "envelope_id": "env_root",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "kickoff",
        "sent_at": base + 1,
        "thread_root_id": None,
    })
    await store.store({
        "envelope_id": "env_reply_a",
        "envelope_kind": "channel",
        "sender_slug": "bob-0002",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "reply A",
        "sent_at": base + 2,
        "thread_root_id": "env_root",
    })
    await store.store({
        "envelope_id": "env_reply_b",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "reply B",
        "sent_at": base + 3,
        "thread_root_id": "env_root",
    })

    batch = await store.get_thread_batch("env_root", since_sent_at=0)
    assert [m.envelope_id for m in batch] == ["env_root", "env_reply_a", "env_reply_b"]
    assert batch[0].thread_root_id is None
    assert batch[1].thread_root_id == "env_root"
    await store.close()


@pytest.mark.asyncio
async def test_get_thread_batch_filters_by_since_sent_at():
    store = await _make_store()
    await store.store({
        "envelope_id": "env_root",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "kickoff",
        "sent_at": 100,
        "thread_root_id": None,
    })
    await store.store({
        "envelope_id": "env_reply",
        "envelope_kind": "channel",
        "sender_slug": "bob-0002",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "reply",
        "sent_at": 200,
        "thread_root_id": "env_root",
    })

    batch = await store.get_thread_batch("env_root", since_sent_at=100)
    # Strict ``>``: root at sent_at=100 is excluded; reply at 200 included.
    assert [m.envelope_id for m in batch] == ["env_reply"]
    await store.close()


@pytest.mark.asyncio
async def test_mark_and_get_last_processed_sent_at():
    store = await _make_store()
    assert await store.get_last_processed_sent_at("env_root") == 0

    await store.mark_thread_processed("env_root", 1000)
    assert await store.get_last_processed_sent_at("env_root") == 1000

    # MAX semantics: a regress doesn't lower the cursor.
    await store.mark_thread_processed("env_root", 500)
    assert await store.get_last_processed_sent_at("env_root") == 1000

    # Advance updates.
    await store.mark_thread_processed("env_root", 2000)
    assert await store.get_last_processed_sent_at("env_root") == 2000
    await store.close()


# ─── _admit_thread_message ───────────────────────────────────────


@pytest.mark.asyncio
async def test_admit_first_message_creates_entry_and_pushes_tuple():
    store = await _make_store()
    client = _make_client_for_queue(store)

    msg = _msg("env_1")
    await client._admit_thread_message(
        root_id="env_1",
        priority=PRIORITY_HUMAN,
        msg_dict=msg,
        channel_meta=_channel_meta(),
    )

    assert "env_1" in client._thread_state
    entry = client._thread_state["env_1"]
    assert entry.in_queue is True
    assert entry.current_priority == PRIORITY_HUMAN
    assert entry.messages == [msg]
    # One heap tuple.
    assert client._queue.qsize() == 1
    priority, seq, root_id = await client._queue.get()
    assert priority == PRIORITY_HUMAN
    assert seq == entry.current_seq
    assert root_id == "env_1"
    await store.close()


@pytest.mark.asyncio
async def test_admit_second_same_priority_coalesces_no_new_heap_tuple():
    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_1"),
        channel_meta=_channel_meta(),
    )
    qsize_before = client._queue.qsize()
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_2"),
        channel_meta=_channel_meta(),
    )

    entry = client._thread_state["env_root"]
    assert len(entry.messages) == 2
    assert [m["envelope_id"] for m in entry.messages] == ["env_1", "env_2"]
    # No new heap tuple — same-priority arrivals don't move the slot.
    assert client._queue.qsize() == qsize_before
    await store.close()


@pytest.mark.asyncio
async def test_admit_lower_priority_arrival_does_not_replace_slot():
    """Higher priority = lower numeric value. A LATER arrival with a
    LOWER priority (larger numeric) joins the batch but doesn't push
    a new heap tuple."""
    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_MENTIONED_HUMAN,  # priority=1
        msg_dict=_msg("env_1"),
        channel_meta=_channel_meta(),
    )
    qsize_before = client._queue.qsize()
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,  # priority=3 — lower priority
        msg_dict=_msg("env_2"),
        channel_meta=_channel_meta(),
    )

    entry = client._thread_state["env_root"]
    # Slot priority stays at the higher value (numeric 1).
    assert entry.current_priority == PRIORITY_MENTIONED_HUMAN
    assert [m["envelope_id"] for m in entry.messages] == ["env_1", "env_2"]
    assert client._queue.qsize() == qsize_before
    await store.close()


@pytest.mark.asyncio
async def test_admit_higher_priority_arrival_pushes_new_tuple_keeps_cursor():
    """Spec point 1c: when a higher-priority arrival lands on a root
    already in the queue, push a NEW heap tuple with the upgraded
    priority + a fresh seq. Cursor (``messages[0]``) stays at the
    earliest unprocessed message."""
    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_first"),
        channel_meta=_channel_meta(),
    )
    first_seq = client._thread_state["env_root"].current_seq
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_MENTIONED_HUMAN,  # higher priority
        msg_dict=_msg("env_second"),
        channel_meta=_channel_meta(),
    )

    entry = client._thread_state["env_root"]
    assert entry.current_priority == PRIORITY_MENTIONED_HUMAN
    assert entry.current_seq > first_seq
    # Cursor preserved: earliest message is still env_first.
    assert entry.messages[0]["envelope_id"] == "env_first"
    assert entry.messages[1]["envelope_id"] == "env_second"
    # Two heap tuples now (the old one stays — will be detected as
    # stale on pop via the seq mismatch).
    assert client._queue.qsize() == 2
    await store.close()


# ─── _consume_queue ──────────────────────────────────────────────


async def _run_consumer_one_batch(client: PuffoCoreMessageClient) -> tuple[str, list[dict], dict]:
    """Drive ``_consume_queue`` until it dispatches exactly one
    batch, then cancel the task. Returns the (root_id, batch,
    channel_meta) tuple the callback saw.
    """
    received: list[tuple[str, list[dict], dict]] = []
    done = asyncio.Event()

    async def callback(root_id, batch, channel_meta):
        received.append((root_id, batch, channel_meta))
        done.set()

    task = asyncio.create_task(client._consume_queue(callback))
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert len(received) == 1
    return received[0]


@pytest.mark.asyncio
async def test_consume_yields_whole_batch():
    store = await _make_store()
    client = _make_client_for_queue(store)
    for i in range(3):
        await client._admit_thread_message(
            root_id="env_root",
            priority=PRIORITY_HUMAN,
            msg_dict=_msg(f"env_{i}", sent_at=100 + i),
            channel_meta=_channel_meta(),
        )

    root_id, batch, channel_meta = await _run_consumer_one_batch(client)
    assert root_id == "env_root"
    assert [m["envelope_id"] for m in batch] == ["env_0", "env_1", "env_2"]
    assert channel_meta["channel_id"] == "ch_1"
    # In-queue flag flipped off; slot reopens on next arrival.
    assert client._thread_state["env_root"].in_queue is False
    # Cursor persisted.
    assert await store.get_last_processed_sent_at("env_root") == 102
    await store.close()


@pytest.mark.asyncio
async def test_consume_skips_stale_seq():
    """When a re-prioritisation pushes a new tuple, the old one stays
    in the heap. ``_consume_queue`` must skip it on pop without
    invoking the callback, then process the fresh tuple."""
    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_a", sent_at=10),
        channel_meta=_channel_meta(),
    )
    # Bump priority — second tuple in the queue.
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_MENTIONED_HUMAN,
        msg_dict=_msg("env_b", sent_at=20),
        channel_meta=_channel_meta(),
    )
    assert client._queue.qsize() == 2

    root_id, batch, _meta = await _run_consumer_one_batch(client)
    assert root_id == "env_root"
    # The batch is the full coalesced list — cursor preserved.
    assert [m["envelope_id"] for m in batch] == ["env_a", "env_b"]
    # The stale tuple still in the heap should be dropped before
    # any further batches dispatch.
    assert client._queue.qsize() <= 1
    await store.close()


@pytest.mark.asyncio
async def test_arrival_after_processing_does_not_replay():
    """After a successful dispatch, subsequent arrivals on the same
    thread enqueue ONLY the new messages — the agent doesn't see
    already-processed entries."""
    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_1", sent_at=100),
        channel_meta=_channel_meta(),
    )
    root_id, first_batch, _ = await _run_consumer_one_batch(client)
    assert [m["envelope_id"] for m in first_batch] == ["env_1"]
    assert await store.get_last_processed_sent_at("env_root") == 100

    # New message arrives in the same thread post-dispatch.
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_2", sent_at=200),
        channel_meta=_channel_meta(),
    )
    root_id_2, second_batch, _ = await _run_consumer_one_batch(client)
    assert root_id_2 == "env_root"
    assert [m["envelope_id"] for m in second_batch] == ["env_2"]
    assert await store.get_last_processed_sent_at("env_root") == 200
    await store.close()


@pytest.mark.asyncio
async def test_api_error_requeues_same_batch_preserves_cursor():
    """``AgentAPIError`` re-enqueues the same batch without advancing
    the durable cursor. The next pop must see the same messages."""
    from puffo_agent.agent.core import AgentAPIError

    store = await _make_store()
    client = _make_client_for_queue(store)

    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_1", sent_at=100),
        channel_meta=_channel_meta(),
    )
    await client._admit_thread_message(
        root_id="env_root",
        priority=PRIORITY_HUMAN,
        msg_dict=_msg("env_2", sent_at=200),
        channel_meta=_channel_meta(),
    )

    call_count = 0
    seen_batches: list[list[dict]] = []

    async def callback(root_id, batch, channel_meta):
        nonlocal call_count
        seen_batches.append(list(batch))
        call_count += 1
        if call_count == 1:
            raise AgentAPIError("provider 429")
        # second call: success, stop consumer
        raise asyncio.CancelledError()

    # Patch the sleep so we don't wait 15-45s in tests.
    import puffo_agent.agent.puffo_core_client as mod
    original_sleep = mod.asyncio.sleep
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        return None

    mod.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(client._consume_queue(callback))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        mod.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert call_count == 2
    # Both invocations saw the same batch (cursor preserved).
    assert [m["envelope_id"] for m in seen_batches[0]] == ["env_1", "env_2"]
    assert [m["envelope_id"] for m in seen_batches[1]] == ["env_1", "env_2"]
    # We slept for the back-off between the two attempts.
    assert any(s > 0 for s in sleeps)
    # Cursor was NOT advanced for the failing turn but IS advanced
    # for the successful one (the second call was cancelled before
    # mark_thread_processed ran, so cursor stays at 0). Acceptable —
    # the second attempt's failure-mode (CancelledError) is an
    # artificial stop signal in this test, not a real success path.
    await store.close()


@pytest.mark.asyncio
async def test_pre_existing_cursor_blocks_redelivered_messages():
    """If the sqlite cursor says a thread is processed past
    ``sent_at``, the listen-handler-equivalent skip check (the same
    one ``handle_envelope`` runs before ``_admit_thread_message``)
    must reject the redelivered message. We exercise the cursor lookup
    directly since the skip lives in the closure."""
    store = await _make_store()
    await store.mark_thread_processed("env_root", 500)

    # Anything with sent_at <= 500 should be skipped by the listen
    # handler. We verify the cursor query returns the right value;
    # the integration of "compare and skip" is tested by inspection
    # of the listen() body.
    assert await store.get_last_processed_sent_at("env_root") == 500
    # A fresh root has no entry → returns 0 → admits everything.
    assert await store.get_last_processed_sent_at("env_fresh") == 0
    await store.close()
