from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import stat
from urllib.parse import parse_qs, urlparse

import pytest

from puffo_agent.agent.message_store import (
    LifecycleConflict,
    MessageStore,
    reminder_time_to_rfc3339,
)
from puffo_agent.agent.reminder_scheduler import ReminderScheduler
from puffo_agent.agent.reminder_sync import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_VERSION,
    REMINDER_PAYLOAD_FORMAT,
    ReminderContractError,
    ReminderEnvelopeError,
    ReminderSync,
    _base64url_encode,
    decrypt_reminder_payload,
    encrypt_reminder_payload,
)
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.keystore import KeyStore


class _RetryThenSnapshotTransport:
    """A deliberately tiny Server-shaped transport used by the contract test."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, dict[str, object]]] = []
        self.snapshot: list[dict[str, object]] = []
        self.fail_first_put = True

    async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
        self.puts.append((path, copy.deepcopy(body)))
        if self.fail_first_put:
            self.fail_first_put = False
            raise OSError("offline")
        return {"occurrence_id": path.rsplit("/", 1)[-1], **body}

    async def get(self, _path: str) -> dict[str, object]:
        return {"occurrences": copy.deepcopy(self.snapshot), "next_after": None}

    async def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        return {
            "occurrence_id": path.split("/")[-2],
            "revision": body["revision"],
            "lifecycle": "scheduled",
            "status": "acquired",
        }


class _RecordingTransport:
    """A small strict-shape double for native/keyless sync tests."""

    def __init__(self, *, keyless: bool = False) -> None:
        self.keyless = keyless
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.rows: list[dict[str, object]] = []
        self.failure: Exception | None = None
        self.snapshot_failure: Exception | None = None
        self.claim_status = "acquired"
        self.claims: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _response(path: str, body: dict[str, object]) -> dict[str, object]:
        response = {"occurrence_id": path.rsplit("/", 1)[-1], **body}
        if body["lifecycle"] != "scheduled":
            response.pop("opaque_payload")
            response.pop("delivery_claim_id", None)
        return response

    async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
        self.calls.append(("put", path, copy.deepcopy(body)))
        if self.failure is not None:
            raise self.failure
        return self._response(path, body)

    async def put_unsigned(
        self, path: str, body: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(("put_unsigned", path, copy.deepcopy(body)))
        if self.failure is not None:
            raise self.failure
        return self._response(path, body)

    async def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        self.claims.append((path, copy.deepcopy(body)))
        if self.failure is not None:
            raise self.failure
        lifecycle = "delivered" if self.claim_status == "terminal" else "scheduled"
        response = {
            "occurrence_id": path.split("/")[-2],
            "revision": body["revision"] + (1 if self.claim_status == "terminal" else 0),
            "lifecycle": lifecycle,
            "status": self.claim_status,
        }
        if self.claim_status == "acquired":
            response["lease_expires_at"] = "2030-01-01T00:00:00.123456Z"
        return response

    async def get(self, path: str) -> dict[str, object]:
        self.calls.append(("get", path, None))
        if self.snapshot_failure is not None:
            raise self.snapshot_failure
        return self._snapshot(path)

    async def get_unsigned(self, path: str) -> dict[str, object]:
        self.calls.append(("get_unsigned", path, None))
        if self.snapshot_failure is not None:
            raise self.snapshot_failure
        return self._snapshot(path)

    def _snapshot(self, path: str) -> dict[str, object]:
        query = parse_qs(urlparse(path).query)
        after = query.get("after", [None])[0]
        ordered = sorted(self.rows, key=lambda row: str(row["occurrence_id"]))
        start = 0
        if after is not None:
            start = next(
                index + 1
                for index, row in enumerate(ordered)
                if row["occurrence_id"] == after
            )
        page = ordered[start:start + 1]
        next_after = page[-1]["occurrence_id"] if start + 1 < len(ordered) else None
        return {"occurrences": copy.deepcopy(page), "next_after": next_after}


def _remote_row(
    keys: KeyStore,
    *,
    owner: str = "agent-a",
    reminder_id: str,
    occurrence_id: str,
    target: str = "dm:peer-a",
    content: str = "private remote content",
    intended_at_ms: int = 1_000,
    lifecycle_at_ms: int = 500,
    revision: int = 1,
) -> dict[str, object]:
    envelope = encrypt_reminder_payload(
        dek=keys.load_or_create_message_backup_dek(owner),
        owner_slug=owner,
        reminder_id=reminder_id,
        occurrence_id=occurrence_id,
        intended_at_ms=intended_at_ms,
        target=target,
        content=content,
    )
    return {
        "occurrence_id": occurrence_id,
        "revision": revision,
        "reminder_id": reminder_id,
        "due_at": reminder_time_to_rfc3339(intended_at_ms),
        "lifecycle": "scheduled",
        "lifecycle_at": reminder_time_to_rfc3339(lifecycle_at_ms),
        "payload_format": REMINDER_PAYLOAD_FORMAT,
        "opaque_payload": _base64url_encode(envelope),
    }


def _terminal_row(
    *,
    reminder_id: str,
    occurrence_id: str,
    intended_at_ms: int = 1_000,
    lifecycle: str = "delivered",
    lifecycle_at_ms: int = 1_500,
    revision: int = 2,
) -> dict[str, object]:
    return {
        "occurrence_id": occurrence_id,
        "revision": revision,
        "reminder_id": reminder_id,
        "due_at": reminder_time_to_rfc3339(intended_at_ms),
        "lifecycle": lifecycle,
        "lifecycle_at": reminder_time_to_rfc3339(lifecycle_at_ms),
        "payload_format": REMINDER_PAYLOAD_FORMAT,
    }


def _sync(
    tmp_path,
    transport: object,
    *,
    name: str = "messages.db",
    keys: KeyStore | None = None,
    now: list[int] | None = None,
    wakes: list[str] | None = None,
    idle_seconds: float = 30.0,
) -> tuple[ReminderSync, MessageStore, ReminderScheduler, KeyStore]:
    now = now if now is not None else [2_000]
    keys = keys or KeyStore(tmp_path / "agent-state" / "keys")
    store = MessageStore(tmp_path / name, now_ms=lambda: now[0])
    scheduler = ReminderScheduler(
        store=store,
        notify=lambda: (wakes if wakes is not None else []).append("wake"),
        now_ms=lambda: now[0],
    )
    return (
        ReminderSync(
            store=store,
            keystore=keys,
            owner_slug="agent-a",
            http_client=transport,
            scheduler=scheduler,
            now_ms=lambda: now[0],
            idle_seconds=idle_seconds,
        ),
        store,
        scheduler,
        keys,
    )


@pytest.mark.asyncio
async def test_lifecycle_commits_wake_a_sleeping_sync_outbox(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(
        tmp_path, transport, idle_seconds=3_600,
    )
    scheduler.set_lifecycle_committed_callback(sync.signal_lifecycle_committed)
    task = asyncio.create_task(sync.run(request_snapshot_on_start=False))
    await asyncio.sleep(0.05)

    async def wait_for_put_count(count: int) -> None:
        while sum(call[0] == "put" for call in transport.calls) < count:
            await asyncio.sleep(0)

    created = await scheduler.create_reminder(
        content="sync now",
        target="dm:peer",
        intended_at="1970-01-01T00:00:05Z",
    )
    await asyncio.wait_for(wait_for_put_count(1), timeout=1)
    record = await store.get_reminder_sync_record(str(created["occurrence_id"]))
    assert record is not None and record.server_ack_revision == 1

    await scheduler.cancel_reminder(reminder_id=str(created["reminder_id"]))
    await asyncio.wait_for(wait_for_put_count(2), timeout=1)
    record = await store.get_reminder_sync_record(str(created["occurrence_id"]))
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "cancelled", 2, 2,
    )

    sync.stop()
    await task
    await store.close()


@pytest.mark.asyncio
async def test_encrypted_retry_reconstructs_overdue_occurrence(tmp_path):
    """The v1 public seam is local-first, byte-stable, and reconstructable."""
    now = [2_000]
    keys = KeyStore(tmp_path / "agent-state" / "keys")
    transport = _RetryThenSnapshotTransport()
    source = MessageStore(tmp_path / "source" / "messages.db", now_ms=lambda: now[0])
    source_scheduler = ReminderScheduler(
        store=source, notify=lambda: None, now_ms=lambda: now[0],
    )
    source_sync = ReminderSync(
        store=source,
        keystore=keys,
        owner_slug="agent-a",
        http_client=transport,
        scheduler=source_scheduler,
        now_ms=lambda: now[0],
    )

    reminder = await source.create_reminder(
        reminder_id="reminder-a",
        occurrence_id="occurrence-a",
        target="dm:peer-a",
        content="private launch checklist",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    await source_sync.upload_pending_once()
    await source_sync.upload_pending_once(force=True)

    assert len(transport.puts) == 2
    assert transport.puts[0][1]["opaque_payload"] == transport.puts[1][1]["opaque_payload"]
    assert "private launch checklist" not in repr(transport.puts)
    assert "dm:peer-a" not in repr(transport.puts)
    transport.snapshot = [{"occurrence_id": reminder.occurrence_id, **transport.puts[-1][1]}]

    restored = MessageStore(tmp_path / "restored" / "messages.db", now_ms=lambda: now[0])
    wakes: list[str] = []
    restored_scheduler = ReminderScheduler(
        store=restored, notify=lambda: wakes.append("wake"), now_ms=lambda: now[0],
    )
    restored_sync = ReminderSync(
        store=restored,
        keystore=keys,
        owner_slug="agent-a",
        http_client=transport,
        scheduler=restored_scheduler,
        now_ms=lambda: now[0],
    )
    assert await restored_sync.reconcile_snapshot() == 1
    restored_scheduler.set_delivery_authorizer(restored_sync.authorize_due_delivery)
    delivered = await restored_scheduler.process_due_once()

    assert [item.occurrence_id for item in delivered] == ["occurrence-a"]
    assert [item.envelope_id for item in await restored.get_pending()] == [
        "reminder-occurrence:occurrence-a"
    ]
    assert wakes == ["wake"]
    await source.close()
    await restored.close()


@pytest.mark.asyncio
async def test_acknowledged_due_reminder_is_blocked_until_snapshot_ready(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    transport.snapshot_failure = OSError("snapshot unavailable")
    with pytest.raises(OSError):
        await sync.reconcile_snapshot()
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)

    assert await scheduler.process_due_once() == ()
    assert await store.get_message_by_envelope(
        f"reminder-occurrence:{reminder.occurrence_id}"
    ) is None
    assert transport.claims == []
    await store.close()


@pytest.mark.asyncio
async def test_successful_put_with_local_ack_loss_blocks_prepared_row_until_snapshot(
    tmp_path, monkeypatch,
):
    transport = _RecordingTransport()
    sync, store, _scheduler, keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )

    async def lose_local_ack(**_kwargs) -> bool:
        raise OSError("local acknowledgement lost")

    monkeypatch.setattr(store, "acknowledge_reminder_sync_revision", lose_local_ack)
    with pytest.raises(OSError, match="local acknowledgement lost"):
        await sync.upload_pending_once()
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None
    assert (
        record.server_ack_revision,
        record.payload_format,
        record.opaque_payload is not None,
    ) == (0, REMINDER_PAYLOAD_FORMAT, True)
    assert [call[0] for call in transport.calls] == ["put"]
    await store.close()

    resumed, reopened, resumed_scheduler, _keys = _sync(
        tmp_path, transport, keys=keys,
    )
    transport.snapshot_failure = OSError("snapshot unavailable")
    with pytest.raises(OSError, match="snapshot unavailable"):
        await resumed.reconcile_snapshot()
    resumed_scheduler.set_delivery_authorizer(resumed.authorize_due_delivery)

    assert await resumed_scheduler.process_due_once() == ()
    assert transport.claims == []
    record = await reopened.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None and record.server_ack_revision == 0
    assert record.payload_format == REMINDER_PAYLOAD_FORMAT
    assert record.opaque_payload is not None
    assert await reopened.get_message_by_envelope(
        f"reminder-occurrence:{reminder.occurrence_id}"
    ) is None
    await reopened.close()


@pytest.mark.asyncio
async def test_prepared_unknown_ack_reminder_claims_once_after_snapshot_is_ready(
    tmp_path, monkeypatch,
):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )

    async def lose_local_ack(**_kwargs) -> bool:
        raise OSError("local acknowledgement lost")

    monkeypatch.setattr(store, "acknowledge_reminder_sync_revision", lose_local_ack)
    with pytest.raises(OSError, match="local acknowledgement lost"):
        await sync.upload_pending_once()
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None and record.server_ack_revision == 0
    assert record.payload_format == REMINDER_PAYLOAD_FORMAT
    assert record.opaque_payload is not None

    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        reminder.occurrence_id
    ]
    assert len(transport.claims) == 1
    assert await scheduler.process_due_once() == ()
    assert [item.envelope_id for item in await store.get_pending()] == [
        f"reminder-occurrence:{reminder.occurrence_id}"
    ]
    await store.close()


@pytest.mark.asyncio
async def test_acquired_claim_delivers_and_terminal_put_carries_same_claim_id(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    scheduler.set_lifecycle_committed_callback(sync.signal_lifecycle_committed)
    sync._wakeup.clear()

    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        reminder.occurrence_id
    ]
    claim_id = transport.claims[0][1]["claim_id"]
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None and record.delivery_claim_id == claim_id
    assert record.delivery_claim_acquired
    assert sync._wakeup.is_set()
    assert await sync.upload_pending_once() == 1
    delivered_put = transport.calls[-1][2]
    assert delivered_put is not None and delivered_put["delivery_claim_id"] == claim_id
    await store.close()


@pytest.mark.asyncio
async def test_persisted_acquired_claim_is_revalidated_before_delivery(tmp_path):
    transport = _RecordingTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None

    first = await sync.authorize_due_delivery((record,))
    assert first.occurrence_ids == (reminder.occurrence_id,)
    acquired = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert acquired is not None and acquired.delivery_claim_acquired

    transport.claim_status = "held"
    second = await sync.authorize_due_delivery((acquired,))
    assert second.occurrence_ids == ()
    assert len(transport.claims) == 2
    assert transport.claims[0][1]["claim_id"] == transport.claims[1][1]["claim_id"]
    await store.close()


@pytest.mark.asyncio
async def test_held_or_terminal_claim_never_creates_a_local_inbox_event(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    commits: list[str] = []
    scheduler.set_lifecycle_committed_callback(lambda: commits.append("committed"))
    transport.claim_status = "held"
    assert await scheduler.process_due_once() == ()
    assert commits == []
    assert await store.get_message_by_envelope(f"reminder-occurrence:{reminder.occurrence_id}") is None

    transport.claim_status = "terminal"
    transport.rows = [_terminal_row(
        reminder_id=reminder.reminder_id,
        occurrence_id=reminder.occurrence_id,
    )]
    assert await scheduler.process_due_once() == ()
    assert (await store.get_reminder(reminder.reminder_id)).state == "delivered"
    assert transport.claims[-1][1]["revision"] == 1
    assert await store.get_message_by_envelope(f"reminder-occurrence:{reminder.occurrence_id}") is None
    await store.close()


@pytest.mark.asyncio
async def test_acknowledged_preclaim_row_can_acquire_delivery_claim(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    await store.claim_due_reminders(now_ms=2_000)
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)

    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        reminder.occurrence_id
    ]
    assert len(transport.claims) == 1
    await store.close()


@pytest.mark.asyncio
async def test_acknowledged_persisted_delivery_claim_rejects_cancellation(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    transport.claim_status = "held"
    assert await scheduler.process_due_once() == ()

    with pytest.raises(LifecycleConflict):
        await store.cancel_reminder(reminder.reminder_id)
    assert (await store.get_reminder(reminder.reminder_id)).state == "scheduled"
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_status", ["held", "acquired"])
async def test_unknown_ack_persisted_delivery_claim_rejects_cancellation(
    tmp_path, monkeypatch, claim_status,
):
    transport = _RecordingTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    assert await sync.reconcile_snapshot() == 0
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )

    async def lose_local_ack(**_kwargs) -> bool:
        raise OSError("local acknowledgement lost")

    monkeypatch.setattr(store, "acknowledge_reminder_sync_revision", lose_local_ack)
    with pytest.raises(OSError, match="local acknowledgement lost"):
        await sync.upload_pending_once()
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None and record.server_ack_revision == 0

    transport.claim_status = claim_status
    authorization = await sync.authorize_due_delivery((record,))
    if claim_status == "acquired":
        assert authorization.occurrence_ids == (reminder.occurrence_id,)
    else:
        assert authorization.occurrence_ids == ()

    with pytest.raises(LifecycleConflict):
        await store.cancel_reminder(reminder.reminder_id)
    retained = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert retained is not None
    assert retained.state == "scheduled"
    assert retained.delivery_claim_id is not None
    assert retained.delivery_claim_acquired is (claim_status == "acquired")
    await store.close()


@pytest.mark.asyncio
async def test_delivery_claim_id_survives_restart_before_claim_response(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        content="due", target="dm:peer", intended_at_ms=1_000,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    transport.failure = OSError("offline")
    assert await scheduler.process_due_once() == ()
    first_claim_id = transport.claims[-1][1]["claim_id"]
    await store.close()

    transport.failure = None
    resumed, reopened, resumed_scheduler, _keys = _sync(
        tmp_path, transport, keys=keys,
    )
    assert await resumed.reconcile_snapshot() == 0
    resumed_scheduler.set_delivery_authorizer(resumed.authorize_due_delivery)
    assert [item.occurrence_id for item in await resumed_scheduler.process_due_once()] == [
        reminder.occurrence_id
    ]
    assert transport.claims[-1][1]["claim_id"] == first_claim_id
    await reopened.close()


@pytest.mark.asyncio
async def test_only_acquired_replica_delivers_shared_acknowledged_occurrence(tmp_path):
    transport = _RecordingTransport()
    first, first_store, first_scheduler, keys = _sync(tmp_path, transport, name="first.db")
    second, second_store, second_scheduler, _keys = _sync(
        tmp_path, transport, name="second.db", keys=keys,
    )
    for store in (first_store, second_store):
        await store.create_reminder(
            reminder_id="reminder-shared",
            occurrence_id="occurrence-shared",
            content="due", target="dm:peer", intended_at_ms=1_000,
        )
    assert await first.upload_pending_once() == 1
    assert await second.upload_pending_once() == 1
    assert await first.reconcile_snapshot() == 0
    assert await second.reconcile_snapshot() == 0
    first_scheduler.set_delivery_authorizer(first.authorize_due_delivery)
    second_scheduler.set_delivery_authorizer(second.authorize_due_delivery)

    assert [item.occurrence_id for item in await first_scheduler.process_due_once()] == [
        "occurrence-shared"
    ]
    transport.claim_status = "held"
    assert await second_scheduler.process_due_once() == ()
    assert await first_store.get_message_by_envelope("reminder-occurrence:occurrence-shared")
    assert await second_store.get_message_by_envelope("reminder-occurrence:occurrence-shared") is None
    await first_store.close()
    await second_store.close()


def test_dek_is_private_stable_and_scoped_to_agent_state(tmp_path, caplog):
    keys_a = KeyStore(tmp_path / "agent-a" / "keys")
    first = keys_a.load_or_create_message_backup_dek("agent-a")
    path = keys_a._message_backup_dek_path("agent-a")

    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert KeyStore(tmp_path / "agent-a" / "keys").load_or_create_message_backup_dek(
        "agent-a"
    ) == first
    second = KeyStore(tmp_path / "agent-b" / "keys").load_or_create_message_backup_dek(
        "agent-b"
    )
    assert second != first
    assert "messages.db" not in str(path)
    assert first.hex() not in caplog.text


def test_envelope_round_trip_nonce_and_aad_tamper_fail_closed():
    dek = os.urandom(32)
    common = {
        "dek": dek,
        "owner_slug": "agent-a",
        "reminder_id": "reminder-a",
        "occurrence_id": "occurrence-a",
        "intended_at_ms": 1_000,
        "target": "channel:space-a:channel-a",
        "content": "exact private reminder",
    }
    first = encrypt_reminder_payload(**common)
    second = encrypt_reminder_payload(**{**common, "occurrence_id": "occurrence-b"})
    parsed = json.loads(first)

    assert parsed["version"] == ENVELOPE_VERSION
    assert parsed["algorithm"] == ENVELOPE_ALGORITHM
    assert len(base64.urlsafe_b64decode(parsed["nonce"] + "==")) == 12
    assert parsed["nonce"] != json.loads(second)["nonce"]
    assert first != second
    assert decrypt_reminder_payload(
        **{key: value for key, value in common.items() if key not in {"target", "content"}},
        envelope=first,
    ) == ("channel:space-a:channel-a", "exact private reminder")

    for field, changed in (
        ("owner_slug", "agent-b"),
        ("reminder_id", "reminder-b"),
        ("occurrence_id", "occurrence-b"),
        ("intended_at_ms", 2_000),
    ):
        args = {key: value for key, value in common.items() if key not in {"target", "content"}}
        args[field] = changed
        with pytest.raises(ReminderEnvelopeError) as exc_info:
            decrypt_reminder_payload(**args, envelope=first)
        assert "exact private reminder" not in str(exc_info.value)

    for field, changed in (
        ("nonce", _base64url_encode(os.urandom(12))),
        ("ciphertext", _base64url_encode(os.urandom(32))),
        ("algorithm", "wrong-aead"),
        ("version", ENVELOPE_VERSION + 1),
    ):
        tampered = dict(parsed)
        tampered[field] = changed
        envelope = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(ReminderEnvelopeError):
            decrypt_reminder_payload(
                **{key: value for key, value in common.items() if key not in {"target", "content"}},
                envelope=envelope,
            )


@pytest.mark.asyncio
async def test_privacy_request_contains_only_opaque_v1_metadata(tmp_path, caplog):
    transport = _RecordingTransport()
    wakes: list[str] = []
    sync, store, _scheduler, keys = _sync(tmp_path, transport, wakes=wakes)
    await store.create_reminder(
        reminder_id="reminder-private",
        occurrence_id="occurrence-private",
        target="dm:peer-private",
        content="CONTENT_SENTINEL_NEVER_REMOTE",
        intended_at_ms=1_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 1
    _method, path, body = transport.calls[0]
    rendered = repr(body)
    assert path == "/v2/agent-runtime/reminder-occurrences/occurrence-private"
    assert set(body or {}) == {
        "revision", "reminder_id", "due_at", "lifecycle", "lifecycle_at",
        "payload_format", "opaque_payload",
    }
    assert "CONTENT_SENTINEL_NEVER_REMOTE" not in rendered
    assert "dm:peer-private" not in rendered
    assert keys.load_or_create_message_backup_dek("agent-a").hex() not in rendered
    assert "CONTENT_SENTINEL_NEVER_REMOTE" not in caplog.text
    assert "dm:peer-private" not in caplog.text
    assert wakes == []
    await store.close()


@pytest.mark.asyncio
async def test_upload_rejects_unexpected_response_fields_without_leaking_them(
    tmp_path, caplog,
):
    class _ExtraFieldTransport(_RecordingTransport):
        async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(("put", path, copy.deepcopy(body)))
            return {**self._response(path, body), "target": "RESPONSE_SECRET"}

    transport = _ExtraFieldTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    await store.create_reminder(
        reminder_id="reminder-response-contract",
        occurrence_id="occurrence-response-contract",
        target="dm:peer-a",
        content="LOCAL_SECRET",
        intended_at_ms=3_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-response-contract")
    assert record is not None
    assert record.sync_permanent_code == "response_contract"
    assert "RESPONSE_SECRET" not in caplog.text
    assert "LOCAL_SECRET" not in caplog.text
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_revision", [True, 1.0], ids=["bool", "float"])
async def test_upload_rejects_non_integer_response_revision_as_permanent(
    tmp_path, invalid_revision,
):
    class _InvalidRevisionTransport(_RecordingTransport):
        async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(("put", path, copy.deepcopy(body)))
            return {**self._response(path, body), "revision": invalid_revision}

    transport = _InvalidRevisionTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    await store.create_reminder(
        reminder_id="reminder-invalid-revision",
        occurrence_id="occurrence-invalid-revision",
        target="dm:peer-a",
        content="local revision privacy",
        intended_at_ms=3_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-invalid-revision")
    assert record is not None
    assert (
        record.server_ack_revision,
        record.sync_permanent_revision,
        record.sync_permanent_code,
        record.sync_retry_count,
        record.sync_retry_after_ms,
    ) == (0, 1, "response_contract", 0, None)
    await store.close()


@pytest.mark.asyncio
async def test_keyless_missing_unsigned_put_is_permanent_not_retryable(tmp_path):
    class _MissingKeylessPutTransport:
        keyless = True

    sync, store, _scheduler, _keys = _sync(tmp_path, _MissingKeylessPutTransport())
    await store.create_reminder(
        reminder_id="reminder-missing-keyless-put",
        occurrence_id="occurrence-missing-keyless-put",
        target="dm:peer-a",
        content="local keyless contract",
        intended_at_ms=3_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-missing-keyless-put")
    assert record is not None
    assert (
        record.server_ack_revision,
        record.sync_permanent_revision,
        record.sync_permanent_code,
        record.sync_retry_count,
        record.sync_retry_after_ms,
    ) == (0, 1, "transport_contract", 0, None)
    assert await sync.upload_pending_once(force=True) == 0
    await store.close()


@pytest.mark.asyncio
async def test_untouched_local_reminder_delivers_with_real_authorizer_while_offline(
    tmp_path,
):
    """Only work not yet prepared for sync may use the offline local path."""
    transport = _RecordingTransport()
    transport.failure = OSError("offline")
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    reminder = await store.create_reminder(
        reminder_id="reminder-offline",
        occurrence_id="occurrence-offline",
        target="dm:peer-a",
        content="offline local delivery",
        intended_at_ms=1_000,
        created_at_ms=500,
    )

    transport.snapshot_failure = OSError("snapshot unavailable")
    with pytest.raises(OSError, match="snapshot unavailable"):
        await sync.reconcile_snapshot()
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        reminder.occurrence_id
    ]
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "delivered", 2, 0,
    )
    assert [item.envelope_id for item in await store.get_pending()] == [
        "reminder-occurrence:occurrence-offline"
    ]
    assert transport.claims == []
    assert record.payload_format is None and record.opaque_payload is None
    await store.close()


@pytest.mark.asyncio
async def test_upload_claimed_maps_to_scheduled_and_initial_terminal_uploads(tmp_path):
    transport = _RecordingTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    terminal = await store.create_reminder(
        reminder_id="reminder-terminal",
        occurrence_id="occurrence-terminal",
        target="dm:peer-b",
        content="terminal private",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    await store.deliver_due_reminders(now_ms=2_000)
    claimed = await store.create_reminder(
        reminder_id="reminder-claimed",
        occurrence_id="occurrence-claimed",
        target="dm:peer-a",
        content="claimed private",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    await store.claim_due_reminders(now_ms=2_000)

    assert await sync.upload_pending_once() == 2
    by_id = {path.rsplit("/", 1)[-1]: body for _method, path, body in transport.calls}
    assert by_id[claimed.occurrence_id]["revision"] == 1
    assert by_id[claimed.occurrence_id]["lifecycle"] == "scheduled"
    assert by_id[terminal.occurrence_id]["revision"] == 2
    assert by_id[terminal.occurrence_id]["lifecycle"] == "delivered"
    await store.close()


@pytest.mark.asyncio
async def test_upload_race_does_not_ack_newer_local_cancel(tmp_path):
    now = [2_000]

    class _RaceTransport(_RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.store: MessageStore | None = None

        async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(("put", path, copy.deepcopy(body)))
            assert self.store is not None
            await self.store.cancel_reminder("reminder-race", cancelled_at_ms=1_500)
            return self._response(path, body)

    transport = _RaceTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport, now=now)
    transport.store = store
    await store.create_reminder(
        reminder_id="reminder-race",
        occurrence_id="occurrence-race",
        target="dm:peer-a",
        content="race private",
        intended_at_ms=3_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-race")
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "cancelled", 2, 0,
    )
    assert len(await store.pending_reminder_sync_records(now_ms=now[0], force=True)) == 1
    await store.close()


@pytest.mark.asyncio
async def test_envelope_fence_blocks_upload_race_from_local_delivery(tmp_path):
    now = [2_000]

    class _RaceTransport(_RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            self.store: MessageStore | None = None

        async def put(self, path: str, body: dict[str, object]) -> dict[str, object]:
            self.calls.append(("put", path, copy.deepcopy(body)))
            assert self.store is not None
            await self.store.deliver_due_reminders(now_ms=2_000)
            return self._response(path, body)

    transport = _RaceTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport, now=now)
    transport.store = store
    await store.create_reminder(
        reminder_id="reminder-delivery-race",
        occurrence_id="occurrence-delivery-race",
        target="dm:peer-a",
        content="delivery race private",
        intended_at_ms=1_000,
        created_at_ms=500,
    )

    assert await sync.upload_pending_once() == 1
    record = await store.get_reminder_sync_record("occurrence-delivery-race")
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "scheduled", 1, 1,
    )
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_retry_and_permanent_error_are_durable_and_sanitized(tmp_path, caplog):
    transport = _RecordingTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    await store.create_reminder(
        reminder_id="reminder-error",
        occurrence_id="occurrence-error",
        target="dm:peer-a",
        content="SECRET_ERROR_SENTINEL",
        intended_at_ms=3_000,
        created_at_ms=500,
    )
    transport.failure = HttpError(429, "SECRET_ERROR_SENTINEL request echo")
    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-error")
    assert record is not None and record.sync_retry_count == 1
    assert record.sync_retry_after_ms is not None
    assert "SECRET_ERROR_SENTINEL" not in caplog.text

    transport.failure = HttpError(503, "SECRET_ERROR_SENTINEL unavailable")
    assert await sync.upload_pending_once(force=True) == 0
    record = await store.get_reminder_sync_record("occurrence-error")
    assert record is not None and record.sync_retry_count == 2

    transport.failure = HttpError(409, "SECRET_ERROR_SENTINEL conflict echo")
    assert await sync.upload_pending_once(force=True) == 0
    record = await store.get_reminder_sync_record("occurrence-error")
    assert record is not None
    assert record.sync_permanent_revision == 1
    assert record.sync_permanent_code == "http_409"
    calls_before = len(transport.calls)
    assert await sync.upload_pending_once(force=True) == 0
    assert len(transport.calls) == calls_before
    # A completed startup/reconnect reconciliation is the only bounded path
    # allowed to reconsider this same permanent revision.
    transport.failure = None
    assert await sync.upload_pending_once(retry_permanent=True) == 1
    assert "SECRET_ERROR_SENTINEL" not in caplog.text
    await store.close()


@pytest.mark.asyncio
async def test_completed_snapshot_signal_retries_a_permanent_revision_once(tmp_path):
    transport = _RecordingTransport()
    sync, store, _scheduler, _keys = _sync(tmp_path, transport)
    await store.create_reminder(
        reminder_id="reminder-reconcile-retry",
        occurrence_id="occurrence-reconcile-retry",
        target="dm:peer-a",
        content="retry after reconnect",
        intended_at_ms=3_000,
        created_at_ms=500,
    )
    transport.failure = HttpError(409, "conflict")
    assert await sync.upload_pending_once() == 0
    record = await store.get_reminder_sync_record("occurrence-reconcile-retry")
    assert record is not None and record.sync_permanent_revision == 1

    transport.failure = None
    sync.signal_snapshot()
    task = asyncio.create_task(sync.run())
    try:
        for _ in range(50):
            record = await store.get_reminder_sync_record("occurrence-reconcile-retry")
            if record is not None and record.server_ack_revision == 1:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("completed snapshot did not retry the permanent revision")
    finally:
        sync.stop()
        await task
    assert len([call for call in transport.calls if call[0] == "put"]) == 2
    await store.close()


@pytest.mark.asyncio
async def test_native_and_keyless_upload_paths_have_the_same_body_contract(tmp_path):
    for keyless, expected_put, expected_get, expected_prefix in (
        (False, "put", "get", "/v2/agent-runtime/reminder-occurrences/"),
        (True, "put_unsigned", "get_unsigned", "/v2/cloud-agents/agent-runtime/reminder-occurrences/"),
    ):
        transport = _RecordingTransport(keyless=keyless)
        sync, store, _scheduler, _keys = _sync(
            tmp_path / ("bridge" if keyless else "native"), transport,
        )
        await store.create_reminder(
            reminder_id="reminder-route",
            occurrence_id="occurrence-route",
            target="dm:peer-a",
            content="route private",
            intended_at_ms=3_000,
            created_at_ms=500,
        )
        assert await sync.upload_pending_once() == 1
        method, path, body = transport.calls[0]
        assert method == expected_put
        assert path.startswith(expected_prefix)
        assert body and body["payload_format"] == REMINDER_PAYLOAD_FORMAT
        assert await sync.reconcile_snapshot() == 0
        method, path, body = transport.calls[-1]
        assert method == expected_get
        assert path.startswith(expected_prefix.rstrip("/") + "?")
        assert body is None
        await store.close()


@pytest.mark.asyncio
async def test_snapshot_reconstructs_every_page_and_signals_once(tmp_path):
    transport = _RecordingTransport()
    now = [2_000]
    sync, store, scheduler, keys = _sync(tmp_path, transport, now=now)
    signals: list[str] = []
    scheduler.signal = lambda: signals.append("signal")  # type: ignore[method-assign]
    transport.rows = [
        _remote_row(
            keys, reminder_id=f"reminder-{index}", occurrence_id=f"occurrence-{index}",
            content=f"private {index}",
        )
        for index in ("a", "b", "c")
    ]

    assert await sync.reconcile_snapshot() == 3
    assert [item.occurrence_id for item in await store.list_reminders()] == [
        "occurrence-a", "occurrence-b", "occurrence-c",
    ]
    assert len([call for call in transport.calls if call[0] == "get"]) == 3
    assert signals == ["signal"]
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_cursor_before_materializing(tmp_path):
    transport = _RecordingTransport()
    sync, store, _scheduler, keys = _sync(tmp_path, transport)
    transport.rows = [
        _remote_row(
            keys, reminder_id="reminder-bad-page", occurrence_id="occurrence-bad-page",
        )
    ]

    original_snapshot = transport._snapshot

    def malformed_snapshot(path: str):
        result = original_snapshot(path)
        result["next_after"] = "different-cursor"
        return result

    transport._snapshot = malformed_snapshot  # type: ignore[method-assign]
    with pytest.raises(ReminderContractError, match="invalid reminder snapshot"):
        await sync.reconcile_snapshot()
    assert await store.list_reminders() == ()
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_reconstruct_duplicate_restart_overdue_once(tmp_path):
    transport = _RecordingTransport()
    now = [2_000]
    wakes: list[str] = []
    sync, store, scheduler, keys = _sync(tmp_path, transport, now=now, wakes=wakes)
    transport.rows = [
        _remote_row(
            keys,
            reminder_id="reminder-restore",
            occurrence_id="occurrence-restore",
            content="recover exactly once",
        )
    ]

    assert await sync.reconcile_snapshot() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        "occurrence-restore"
    ]
    await store.close()

    reopened = MessageStore(tmp_path / "messages.db", now_ms=lambda: now[0])
    reopened_scheduler = ReminderScheduler(
        store=reopened, notify=lambda: wakes.append("wake"), now_ms=lambda: now[0],
    )
    reopened_sync = ReminderSync(
        store=reopened,
        keystore=keys,
        owner_slug="agent-a",
        http_client=transport,
        scheduler=reopened_scheduler,
        now_ms=lambda: now[0],
    )
    assert await reopened_sync.reconcile_snapshot() == 0
    assert await reopened_scheduler.process_due_once() == ()
    assert [item.envelope_id for item in await reopened.get_pending()] == [
        "reminder-occurrence:occurrence-restore"
    ]
    assert wakes == ["wake"]
    await reopened.close()


@pytest.mark.asyncio
async def test_terminal_snapshot_suppresses_stale_local_schedule_without_event(tmp_path):
    transport = _RecordingTransport()
    now = [2_000]
    sync, store, scheduler, _keys = _sync(tmp_path, transport, now=now)
    reminder = await store.create_reminder(
        reminder_id="reminder-stale-replica",
        occurrence_id="occurrence-stale-replica",
        target="dm:peer-a",
        content="must not fire on the stale replica",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    assert await sync.upload_pending_once() == 1
    transport.rows = [_terminal_row(
        reminder_id=reminder.reminder_id,
        occurrence_id=reminder.occurrence_id,
    )]

    assert await sync.reconcile_snapshot() == 1
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "delivered", 2, 2,
    )
    assert await scheduler.process_due_once() == ()
    assert await store.get_message_by_envelope(
        f"reminder-occurrence:{reminder.occurrence_id}"
    ) is None
    assert await sync.reconcile_snapshot() == 0
    await store.close()


@pytest.mark.asyncio
async def test_remote_cancellation_does_not_ack_local_delivery(tmp_path):
    _syncer, store, _scheduler, _keys = _sync(
        tmp_path,
        _RecordingTransport(),
        now=[2_000],
    )
    reminder = await store.create_reminder(
        reminder_id="reminder-local-delivery",
        occurrence_id="occurrence-local-delivery",
        target="dm:peer-a",
        content="already delivered locally",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    delivered = await store.deliver_due_reminders(now_ms=2_000)
    assert [item.occurrence_id for item in delivered] == [reminder.occurrence_id]

    result = await store.materialize_remote_terminal_reminder(
        reminder_id=reminder.reminder_id,
        occurrence_id=reminder.occurrence_id,
        intended_at_ms=reminder.intended_at_ms,
        lifecycle="cancelled",
        lifecycle_at_ms=1_500,
        revision=2,
        payload_format=REMINDER_PAYLOAD_FORMAT,
    )

    assert result.changed
    record = await store.get_reminder_sync_record(reminder.occurrence_id)
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == (
        "delivered",
        2,
        0,
    )
    pending = await store.pending_reminder_sync_records(force=True)
    assert [item.occurrence_id for item in pending] == [reminder.occurrence_id]
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_never_regresses_terminal_or_deletes_or_overwrites_conflict(tmp_path):
    transport = _RecordingTransport()
    sync, store, _scheduler, keys = _sync(tmp_path, transport)
    local = await store.create_reminder(
        reminder_id="reminder-local",
        occurrence_id="occurrence-local",
        target="dm:peer-a",
        content="local exact",
        intended_at_ms=3_000,
        created_at_ms=500,
    )
    await sync.upload_pending_once()
    await store.cancel_reminder(local.reminder_id, cancelled_at_ms=1_500)
    transport.rows = [
        _remote_row(
            keys,
            reminder_id=local.reminder_id,
            occurrence_id=local.occurrence_id,
            target=local.target,
            content=local.content,
            intended_at_ms=local.intended_at_ms,
            lifecycle_at_ms=local.created_at_ms,
        )
    ]

    assert await sync.reconcile_snapshot() == 0
    record = await store.get_reminder_sync_record(local.occurrence_id)
    assert record is not None
    assert (record.state, record.revision, record.server_ack_revision) == ("cancelled", 2, 1)
    transport.rows = []
    assert await sync.reconcile_snapshot() == 0
    assert (await store.get_reminder(local.reminder_id)).state == "cancelled"

    transport.rows = [
        _remote_row(
            keys,
            reminder_id="reminder-conflict",
            occurrence_id="occurrence-conflict",
            content="remote content", intended_at_ms=4_000,
        )
    ]
    await store.create_reminder(
        reminder_id="reminder-conflict",
        occurrence_id="occurrence-conflict",
        target="dm:peer-a",
        content="local content", intended_at_ms=4_000,
        created_at_ms=500,
    )
    assert await sync.reconcile_snapshot() == 0
    assert (await store.get_reminder("reminder-conflict")).content == "local content"

    claimed = await store.create_reminder(
        reminder_id="reminder-claimed-snapshot",
        occurrence_id="occurrence-claimed-snapshot",
        target="dm:peer-a", content="claimed local", intended_at_ms=1_000,
        created_at_ms=500,
    )
    await store.claim_due_reminders(now_ms=2_000)
    claimed_record = await sync._ensure_envelope(
        (await store.get_reminder_sync_record(claimed.occurrence_id))
    )
    transport.rows = [{
        "occurrence_id": claimed.occurrence_id,
        "revision": 1,
        "reminder_id": claimed.reminder_id,
        "due_at": reminder_time_to_rfc3339(claimed.intended_at_ms),
        "lifecycle": "scheduled",
        "lifecycle_at": reminder_time_to_rfc3339(claimed.created_at_ms),
        "payload_format": REMINDER_PAYLOAD_FORMAT,
        "opaque_payload": _base64url_encode(claimed_record.opaque_payload),
    }]
    assert await sync.reconcile_snapshot() == 1
    assert (await store.get_reminder(claimed.reminder_id)).state == "claimed"
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_never_resurrects_delivered_or_deletes_local_only_work(tmp_path):
    transport = _RecordingTransport()
    sync, store, scheduler, _keys = _sync(tmp_path, transport)
    delivered = await store.create_reminder(
        reminder_id="reminder-delivered-snapshot",
        occurrence_id="occurrence-delivered-snapshot",
        target="dm:peer-a",
        content="delivered local private",
        intended_at_ms=1_000,
        created_at_ms=500,
    )
    assert await sync.upload_pending_once() == 1
    assert await sync.reconcile_snapshot() == 0
    scheduler.set_delivery_authorizer(sync.authorize_due_delivery)
    assert [item.occurrence_id for item in await scheduler.process_due_once()] == [
        delivered.occurrence_id
    ]
    delivered_record = await store.get_reminder_sync_record(delivered.occurrence_id)
    assert delivered_record is not None and delivered_record.opaque_payload is not None
    transport.rows = [{
        "occurrence_id": delivered.occurrence_id,
        "revision": 1,
        "reminder_id": delivered.reminder_id,
        "due_at": reminder_time_to_rfc3339(delivered.intended_at_ms),
        "lifecycle": "scheduled",
        "lifecycle_at": reminder_time_to_rfc3339(delivered.created_at_ms),
        "payload_format": delivered_record.payload_format,
        "opaque_payload": _base64url_encode(delivered_record.opaque_payload),
    }]
    assert await sync.reconcile_snapshot() == 0
    delivered_after = await store.get_reminder_sync_record(delivered.occurrence_id)
    assert delivered_after is not None
    assert (delivered_after.state, delivered_after.revision, delivered_after.server_ack_revision) == (
        "delivered", 2, 1,
    )

    local_only = await store.create_reminder(
        reminder_id="reminder-local-only",
        occurrence_id="occurrence-local-only",
        target="dm:peer-a",
        content="never uploaded private",
        intended_at_ms=9_000,
        created_at_ms=500,
    )
    transport.rows = []
    assert await sync.reconcile_snapshot() == 0
    local_only_after = await store.get_reminder_sync_record(local_only.occurrence_id)
    assert local_only_after is not None
    assert (local_only_after.state, local_only_after.revision, local_only_after.server_ack_revision) == (
        "scheduled", 1, 0,
    )
    await store.close()
