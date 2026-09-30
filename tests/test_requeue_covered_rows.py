"""A failed turn settles the rows it already answered instead of requeueing them."""

import pytest

from puffo_agent.agent.message_store import ProcessingState, ReceiptDisposition
from test_message_store import _channel_payload, _temp_store


@pytest.mark.asyncio
async def test_requeue_settles_covered_rows_and_requeues_only_the_rest():
    store = _temp_store()
    for seq, name in enumerate(("answered", "unanswered"), start=1):
        await store.store_receipt(
            _channel_payload(name),
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="ok",
        )
    await store.admit_messages(
        ["answered", "unanswered"], turn_id="failed", provider_session_id="p"
    )
    await store.add_message_covers(["answered"], source="send", by_envelope_id="reply")

    run = await store.requeue_messages(["unanswered", "answered"], turn_id="failed")

    assert run.state == "requeued"
    assert [m.envelope_id for m in await store.get_pending()] == ["unanswered"]
    answered = await store.get_message_by_envelope("answered")
    assert answered.processing_state == ProcessingState.PROCESSED
    assert answered.processing_turn_id is None and answered.processed_at

    # A turn whose every row was answered leaves nothing pending.
    await store.admit_messages(["unanswered"], turn_id="all-answered", provider_session_id="p")
    await store.add_message_covers(["unanswered"], source="mark")
    await store.requeue_messages(["unanswered"], turn_id="all-answered")
    assert tuple(await store.get_pending()) == ()
    await store.close()


@pytest.mark.asyncio
async def test_cover_after_requeue_settles_previously_admitted_input():
    """A restored model can answer from history before reading Inbox again."""
    store = _temp_store()
    for seq, name in enumerate(("recovered", "fresh"), start=1):
        await store.store_receipt(
            _channel_payload(name), server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE, reason="ok",
        )
    await store.admit_messages(
        ["recovered"], turn_id="cancelled", provider_session_id="before",
    )
    await store.requeue_messages(["recovered"], turn_id="cancelled")

    await store.add_message_covers(
        ["recovered", "fresh"], source="send", by_envelope_id="reply",
    )

    recovered = await store.get_message_by_envelope("recovered")
    assert recovered.processing_state is ProcessingState.PROCESSED
    assert recovered.processed_at and recovered.processing_turn_id is None
    assert [row.envelope_id for row in await store.get_pending()] == ["fresh"]
    await store.admit_messages(
        ["fresh"], turn_id="next", provider_session_id="after",
    )
    assert await store.get_model_visible_through_seq("next", "sp_1", "ch_1") == 2
    await store.close()
