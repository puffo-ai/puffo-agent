"""Cover declarations and uncovered-message renotice lifecycle."""

import os
import tempfile

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.message_store_models import (
    LifecycleConflict,
    ProcessingState,
    ReceiptDisposition,
    _now_ms,
)
from puffo_agent.agent.message_projection import format_message_group


def _temp_store() -> MessageStore:
    d = tempfile.mkdtemp()
    return MessageStore(os.path.join(d, "messages.db"))


def _channel_payload(envelope_id: str, channel_id: str = "ch_1", **kwargs):
    return {
        "envelope_id": envelope_id,
        "envelope_kind": "channel",
        "sender_slug": kwargs.get("sender_slug", "alice-0001"),
        "channel_id": channel_id,
        "space_id": kwargs.get("space_id", "sp_1"),
        "content_type": "text/plain",
        "content": kwargs.get("content", f"Message {envelope_id}"),
        "sent_at": kwargs.get("sent_at", _now_ms()),
        "thread_root_id": kwargs.get("thread_root_id"),
        "reply_to_id": kwargs.get("reply_to_id"),
    }


async def _seed(store: MessageStore, envelope_id: str, seq: int, **kwargs):
    await store.store_receipt(
        _channel_payload(envelope_id, **kwargs),
        server_seq=seq,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="ok",
    )


@pytest.mark.asyncio
async def test_add_message_covers_partitions_known_and_unknown():
    store = _temp_store()
    await _seed(store, "m1", 1)
    await _seed(store, "m2", 2)
    outcome = await store.add_message_covers(
        ["m1", "m2", "ghost"], source="send", by_envelope_id="reply-1",
    )
    assert outcome == {"recorded": ["m1", "m2"], "unknown": ["ghost"]}
    assert await store.get_covered_ids(["m1", "m2", "ghost"]) == {"m1", "m2"}
    # Duplicate declarations collapse instead of erroring.
    again = await store.add_message_covers(
        ["m1"], source="send", by_envelope_id="reply-1",
    )
    assert again["recorded"] == ["m1"]
    assert await store.get_covered_ids(["m1"]) == {"m1"}
    await store.close()


@pytest.mark.asyncio
async def test_add_message_covers_rejects_unknown_source():
    store = _temp_store()
    with pytest.raises(ValueError):
        await store.add_message_covers(["m1"], source="telepathy")
    await store.close()


@pytest.mark.asyncio
async def test_complete_turn_with_renotice_partitions_the_turn():
    store = _temp_store()
    await _seed(store, "covered", 1)
    await _seed(store, "uncovered", 2)
    await store.admit_messages(
        ["covered", "uncovered"], turn_id="turn-1",
        provider_session_id="session-1",
    )
    run = await store.complete_turn_with_renotice(
        ["covered"], ["uncovered"],
        turn_id="turn-1", provider_session_id="session-1",
    )
    assert run.state == ProcessingState.PROCESSED.value

    covered_row = await store.get_message_by_envelope("covered")
    assert covered_row.processing_state is ProcessingState.PROCESSED
    assert not covered_row.renotified

    renotice_row = await store.get_message_by_envelope("uncovered")
    assert renotice_row.processing_state is ProcessingState.PENDING
    assert renotice_row.renotified
    assert renotice_row.processing_turn_id is None
    # Visibility evidence survives renotice: the plaintext already crossed
    # the provider boundary, so the row must not become a freshness blocker.
    assert renotice_row.model_visible_at is not None

    # The renoticed row is deliverable again and completes normally.
    await store.admit_messages(
        ["uncovered"], turn_id="turn-2", provider_session_id="session-1",
    )
    completed = await store.mark_processed(
        ["uncovered"], turn_id="turn-2", provider_session_id="session-1",
    )
    assert completed.state == ProcessingState.PROCESSED.value
    final = await store.get_message_by_envelope("uncovered")
    assert final.renotified  # the one-shot bit survives completion
    await store.close()


@pytest.mark.asyncio
async def test_complete_turn_with_renotice_requires_exact_membership():
    store = _temp_store()
    await _seed(store, "a", 1)
    await _seed(store, "b", 2)
    await store.admit_messages(
        ["a", "b"], turn_id="turn-1", provider_session_id="session-1",
    )
    with pytest.raises(LifecycleConflict):
        await store.complete_turn_with_renotice(
            [], ["a"], turn_id="turn-1", provider_session_id="session-1",
        )
    with pytest.raises(LifecycleConflict):
        await store.complete_turn_with_renotice(
            ["a"], ["a", "b"], turn_id="turn-1",
            provider_session_id="session-1",
        )
    with pytest.raises(ValueError):
        await store.complete_turn_with_renotice(
            ["a", "b"], [], turn_id="turn-1",
            provider_session_id="session-1",
        )
    with pytest.raises(LifecycleConflict):
        await store.complete_turn_with_renotice(
            ["a"], ["b"], turn_id="turn-1", provider_session_id="other",
        )
    # The failed attempts left the turn untouched.
    rows = await store.get_in_turn_messages("turn-1", "session-1")
    assert [row.envelope_id for row in rows] == ["a", "b"]
    await store.close()


@pytest.mark.asyncio
async def test_renotified_row_is_annotated_in_message_projection():
    store = _temp_store()
    await _seed(store, "plain", 1)
    await _seed(store, "redelivered", 2)
    await store.admit_messages(
        ["plain", "redelivered"], turn_id="turn-1",
        provider_session_id="session-1",
    )
    await store.complete_turn_with_renotice(
        ["plain"], ["redelivered"],
        turn_id="turn-1", provider_session_id="session-1",
    )
    row = await store.get_message_by_envelope("redelivered")
    assert "uncovered_redelivery=true" in format_message_group([row])
    plain_row = await store.get_message_by_envelope("plain")
    assert "uncovered_redelivery" not in format_message_group([plain_row])

    # Once the redelivered row settles, history reads stop carrying the
    # marker — it describes a live redelivery attempt, not row history.
    await store.admit_messages(
        ["redelivered"], turn_id="turn-2", provider_session_id="session-1",
    )
    settled = await store.get_message_by_envelope("redelivered")
    assert "uncovered_redelivery=true" in format_message_group([settled])
    await store.mark_processed(
        ["redelivered"], turn_id="turn-2", provider_session_id="session-1",
    )
    settled = await store.get_message_by_envelope("redelivered")
    assert settled.renotified
    assert "uncovered_redelivery" not in format_message_group([settled])
    await store.close()


@pytest.mark.asyncio
async def test_cover_on_renoticed_pending_row_settles_it():
    """mark_covered during the redelivery window terminates the redelivery."""
    store = _temp_store()
    await _seed(store, "asked", 1)
    await store.admit_messages(
        ["asked"], turn_id="turn-1", provider_session_id="session-1",
    )
    await store.complete_turn_with_renotice(
        [], ["asked"], turn_id="turn-1", provider_session_id="session-1",
    )
    row = await store.get_message_by_envelope("asked")
    assert row.processing_state is ProcessingState.PENDING
    assert row.renotified

    outcome = await store.add_message_covers(
        ["asked"], source="mark", note="no reply needed",
    )
    assert outcome["recorded"] == ["asked"]
    row = await store.get_message_by_envelope("asked")
    assert row.processing_state is ProcessingState.PROCESSED

    # A plain PENDING row (never redelivered) is NOT settled by a cover —
    # it still gets its normal first presentation.
    await _seed(store, "fresh", 2)
    await store.add_message_covers(["fresh"], source="mark", note="x")
    fresh = await store.get_message_by_envelope("fresh")
    assert fresh.processing_state is ProcessingState.PENDING
    await store.close()
