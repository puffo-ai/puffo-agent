"""Durable global Inbox lifecycle regressions."""

from __future__ import annotations

import pytest

from puffo_agent.agent.message_store import (
    MessageStore,
    ProcessingState,
    ReceiptDisposition,
    ReceiptWriteStatus,
)


async def _store(tmp_path) -> MessageStore:
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    return store


async def _receipt(store: MessageStore, envelope_id: str, seq: int, sender: str):
    return await store.store_receipt(
        {
            "envelope_id": envelope_id,
            "envelope_kind": "channel",
            "sender_slug": sender,
            "space_id": "space",
            "channel_id": "channel",
            "content_type": "text/plain",
            "content": envelope_id,
            "sent_at": seq,
        },
        server_seq=seq,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )


@pytest.mark.asyncio
async def test_durable_receipt_deduplication(tmp_path):
    store = await _store(tmp_path)
    first = await _receipt(store, "same", 1, "human")
    second = await _receipt(store, "same", 1, "human")
    assert first.status is ReceiptWriteStatus.COMMITTED
    assert second.status is ReceiptWriteStatus.IDEMPOTENT
    assert [row.envelope_id for row in await store.get_pending()] == ["same"]
    await store.close()


@pytest.mark.asyncio
async def test_server_and_local_fifo_ignores_sender_priority(tmp_path):
    store = await _store(tmp_path)
    await _receipt(store, "bot-first", 1, "bot")
    await store.store_local_event(
        {
            "envelope_id": "local-second",
            "envelope_kind": "channel",
            "sender_slug": "system",
            "space_id": "space",
            "channel_id": "channel",
            "content_type": "text/plain",
            "content": "local",
            "sent_at": 2,
        },
        reason="test",
    )
    await _receipt(store, "human-third", 3, "human")
    assert [row.envelope_id for row in await store.get_pending()] == [
        "bot-first",
        "local-second",
        "human-third",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_busy_boundary_restart_and_exact_requeue_lifecycle(tmp_path):
    store = await _store(tmp_path)
    await _receipt(store, "active", 1, "human")
    await store.admit_messages(
        ["active"], turn_id="turn", provider_session_id="session"
    )
    await _receipt(store, "arrived-while-busy", 2, "human")
    assert [row.envelope_id for row in await store.get_pending()] == [
        "arrived-while-busy"
    ]
    await store.close()

    reopened = await _store(tmp_path)
    run = await reopened.get_turn_run("turn")
    assert run is not None
    assert run.state == ProcessingState.IN_TURN.value
    assert run.message_ids == ("active",)
    await reopened.requeue_messages(run.message_ids, turn_id=run.turn_id)
    assert [row.envelope_id for row in await reopened.get_pending()] == [
        "active",
        "arrived-while-busy",
    ]
    await reopened.close()
