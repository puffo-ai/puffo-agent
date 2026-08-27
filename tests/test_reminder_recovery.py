from __future__ import annotations

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.reminder_scheduler import ReminderScheduler
from puffo_agent.agent.reminder_sync import REMINDER_PAYLOAD_FORMAT, ReminderSync
from puffo_agent.crypto.keystore import KeyStore


class _CancelTransport:
    async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
        response = {"occurrence_id": path.rsplit("/", 1)[-1], **body}
        response.pop("opaque_payload")
        response.pop("delivery_claim_id", None)
        return response


@pytest.mark.asyncio
async def test_cancel_requests_snapshot_when_local_materialization_fails(
    tmp_path, monkeypatch,
):
    store = MessageStore(tmp_path / "messages.db", now_ms=lambda: 2_000)
    sync = ReminderSync(
        store=store,
        keystore=KeyStore(tmp_path / "keys"),
        owner_slug="agent-a",
        http_client=_CancelTransport(),
        scheduler=ReminderScheduler(store=store, notify=lambda: None),
        now_ms=lambda: 2_000,
    )
    reminder = await store.create_reminder(
        content="cancel remotely", target="dm:peer", intended_at_ms=5_000,
    )
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None
    await sync._ensure_envelope(record)

    async def fail_materialization(**_kwargs):
        raise OSError("sqlite unavailable")

    monkeypatch.setattr(
        store, "materialize_remote_terminal_reminder", fail_materialization
    )
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await sync.cancel_reminder(reminder.reminder_id)
    assert sync._snapshot_requested
    await store.close()


@pytest.mark.asyncio
async def test_terminal_replacement_response_converges_a_claimed_row(tmp_path):
    store = MessageStore(tmp_path / "messages.db", now_ms=lambda: 2_000)
    old = await store.create_reminder(
        reminder_id="reminder-old",
        occurrence_id="occurrence-old",
        content="old",
        target="dm:peer",
        intended_at_ms=1_000,
    )
    opaque_payload = b"replacement-envelope"
    await store.materialize_remote_scheduled_reminder(
        reminder_id="reminder-new",
        occurrence_id="occurrence-new",
        target="dm:peer",
        content="new",
        intended_at_ms=1_000,
        lifecycle_at_ms=2_000,
        revision=1,
        payload_format=REMINDER_PAYLOAD_FORMAT,
        opaque_payload=opaque_payload,
    )
    claim_id = "50c0a35a-7f24-4a9d-879a-f87ec418b790"
    await store.persist_reminder_delivery_claim_id(
        occurrence_id="occurrence-new", revision=1, claim_id=claim_id,
    )
    assert await store.acquire_reminder_delivery_claim(
        occurrence_id="occurrence-new",
        revision=1,
        claim_id=claim_id,
        lease_expires_at_ms=10_000,
    )
    claimed = await store.claim_authorized_reminders(
        ("occurrence-new",), now_ms=2_000,
    )
    assert claimed[0].state == "claimed"

    _cancelled, replacement = await store.materialize_remote_replacement(
        reminder_id=old.reminder_id,
        occurrence_id=old.occurrence_id,
        cancelled_at_ms=2_000,
        replacement_reminder_id="reminder-new",
        replacement_occurrence_id="occurrence-new",
        target="dm:peer",
        content="new",
        intended_at_ms=1_000,
        replacement_created_at_ms=2_000,
        lifecycle="cancelled",
        lifecycle_at_ms=2_500,
        revision=2,
        payload_format=REMINDER_PAYLOAD_FORMAT,
        opaque_payload=opaque_payload,
    )
    assert (replacement.state, replacement.cancelled_at_ms) == ("cancelled", 2_500)
    record = await store.get_reminder_sync_record("occurrence-new")
    assert record is not None
    assert record.delivery_claim_id is None
    assert not record.delivery_claim_acquired
    await store.close()
