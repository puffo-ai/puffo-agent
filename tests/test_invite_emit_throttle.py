"""PUF-249: TTL-bounded restart-surviving throttle on operator-DM
invite emit.

Sean's daemon emitted the same Jeff→yolo invitation 20× across 8
days because each daemon restart cleared the in-memory
``_processed_invite_ids`` set, and the server-side pending-invite
row never expires. PUF-240 fixed within-process reconnect-clearing;
PUF-249 adds the cross-process restart layer via a sqlite-backed
``invite_emit_throttle`` table on ``MessageStore``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import pytest_asyncio

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import (
    PuffoCoreMessageClient,
    _INVITE_EMIT_THROTTLE_TTL_SECONDS,
)


_TTL_MS = _INVITE_EMIT_THROTTLE_TTL_SECONDS * 1000


def _make_client(
    store: MessageStore,
    operator_slug: str = "op-1",
) -> tuple[PuffoCoreMessageClient, list[dict]]:
    """Bare client harness — same shape as
    ``test_invite_dedup_persistence._make_client`` but wires a real
    ``MessageStore`` so the throttle table is exercised end-to-end."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client._processed_invite_ids = set()
    client._pending_invite_dms = {}
    client._operator_root_pubkey = b"op-root-pk"
    client._inviter_root_cache = {}
    client._invites_response = {"invites": []}
    client.store = store

    sent: list[dict] = []

    async def _stub_send_dm(recipient_slug, text, root_id):
        sent.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_dm_{len(sent)}"}

    async def _stub_space_name(space_id):
        return space_id

    async def _stub_channel_name(*, space_id, channel_id):
        return channel_id

    async def _stub_display_name(slug):
        return ""

    async def _stub_inviter_pk(slug):
        return b"non-operator-pk"

    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]
    client._fetch_inviter_root_pubkey = _stub_inviter_pk  # type: ignore[assignment]

    class _StubHttp:
        async def get(self, path):
            if path.startswith("/invites"):
                return client._invites_response
            return {}

    client.http = _StubHttp()
    client._log = logging.getLogger("test-puf-249")
    return client, sent


def _invite_row(event_id: str = "ev_invite_1") -> dict:
    return {
        "invitation_event_id": event_id,
        "scope": "channel",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "inviter_slug": "non-op",
        "space_name": "Team",
        "channel_name": "general",
    }


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = MessageStore(tmp_path / "messages.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_store_records_and_reads_within_window(store: MessageStore):
    """Direct store-layer pin: a stamped event reads as
    ``was_emitted_within`` for any cutoff up to its stamp."""
    await store.record_invite_emit("ev_1", now_ms=1_000_000)
    assert await store.was_invite_emitted_within(
        "ev_1", ttl_ms=_TTL_MS, now_ms=1_000_500,
    )


@pytest.mark.asyncio
async def test_store_reports_false_after_ttl_expiry(store: MessageStore):
    """Same stamp, now beyond the TTL: read returns False."""
    await store.record_invite_emit("ev_1", now_ms=1_000_000)
    assert not await store.was_invite_emitted_within(
        "ev_1", ttl_ms=_TTL_MS, now_ms=1_000_000 + _TTL_MS + 1,
    )


@pytest.mark.asyncio
async def test_store_unknown_event_reports_false(store: MessageStore):
    """Never-stamped event is always allowed."""
    assert not await store.was_invite_emitted_within(
        "ev_never_seen", ttl_ms=_TTL_MS, now_ms=1_000_000,
    )


@pytest.mark.asyncio
async def test_store_record_is_upsert(store: MessageStore):
    """A later record_invite_emit overwrites the earlier stamp so
    repeated emits don't grow the throttle row."""
    await store.record_invite_emit("ev_1", now_ms=1_000_000)
    await store.record_invite_emit("ev_1", now_ms=2_000_000)
    # Old stamp gone — TTL window from now=2_000_500 still inside.
    assert await store.was_invite_emitted_within(
        "ev_1", ttl_ms=_TTL_MS, now_ms=2_000_500,
    )


@pytest.mark.asyncio
async def test_throttle_survives_simulated_restart(tmp_path: Path):
    """Sean's-symptom regression seal. Open the store, stamp an
    event, close it, reopen at the same path, and assert the stamp
    still reads as within-window. This is the cross-process restart
    case PUF-240 didn't cover and PUF-249 is shipping."""
    db_path = tmp_path / "messages.db"
    store_a = MessageStore(db_path)
    await store_a.open()
    await store_a.record_invite_emit("ev_sean", now_ms=1_000_000)
    await store_a.close()

    store_b = MessageStore(db_path)
    await store_b.open()
    try:
        assert await store_b.was_invite_emitted_within(
            "ev_sean", ttl_ms=_TTL_MS, now_ms=1_000_500,
        )
    finally:
        await store_b.close()


@pytest.mark.asyncio
async def test_first_poll_emits_and_stamps_throttle(store: MessageStore):
    """End-to-end: first poll on a fresh invite fires the DM AND
    records the throttle stamp so a subsequent restart-equivalent
    won't re-emit."""
    client, sent = _make_client(store)
    client._invites_response = {"invites": [_invite_row("ev_a")]}

    await client._poll_pending_invites()

    assert len(sent) == 1
    assert await store.was_invite_emitted_within("ev_a", ttl_ms=_TTL_MS)


@pytest.mark.asyncio
async def test_restart_within_ttl_does_not_re_emit(store: MessageStore):
    """Sean's-symptom regression seal end-to-end. Process A stamps
    the throttle; process B (new client instance, empty in-memory
    dedup) sees the stamp via store and skips the DM."""
    client_a, sent_a = _make_client(store)
    client_a._invites_response = {"invites": [_invite_row("ev_x")]}
    await client_a._poll_pending_invites()
    assert len(sent_a) == 1

    # Simulate a daemon restart: fresh client, empty in-memory dedup,
    # same backing store. The same server row is still pending.
    client_b, sent_b = _make_client(store)
    client_b._invites_response = {"invites": [_invite_row("ev_x")]}
    await client_b._poll_pending_invites()
    assert sent_b == []
    # And the throttle even adds it to the in-memory set on the
    # skip-path so the next within-process poll is a fast no-op.
    assert "ev_x" in client_b._processed_invite_ids


@pytest.mark.asyncio
async def test_restart_after_ttl_does_re_emit(store: MessageStore):
    """The "gentle reminder" intent: 24h+ later, a still-pending
    invite IS re-emitted. The throttle caps frequency, not lifetime."""
    # Stamp at t0.
    await store.record_invite_emit("ev_y", now_ms=1_000_000)

    # Patch _now_ms used by was_invite_emitted_within indirectly: we
    # pass an explicit now_ms via a thin wrapper on the store so the
    # poll path sees "now" as t0 + TTL + 1.
    original = store.was_invite_emitted_within

    async def _was_after_ttl(event_id, ttl_ms):
        return await original(
            event_id, ttl_ms, now_ms=1_000_000 + _TTL_MS + 1,
        )

    store.was_invite_emitted_within = _was_after_ttl  # type: ignore[assignment]

    client, sent = _make_client(store)
    client._invites_response = {"invites": [_invite_row("ev_y")]}
    await client._poll_pending_invites()

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_distinct_invites_each_get_one_prompt(store: MessageStore):
    """Distinct event_ids dedup independently across restart — two
    invites stamped in process A both skip in process B."""
    client_a, sent_a = _make_client(store)
    client_a._invites_response = {
        "invites": [
            _invite_row("ev_a"),
            {**_invite_row("ev_b"), "channel_id": "ch_2"},
        ],
    }
    await client_a._poll_pending_invites()
    assert len(sent_a) == 2

    client_b, sent_b = _make_client(store)
    client_b._invites_response = {
        "invites": [
            _invite_row("ev_a"),
            {**_invite_row("ev_b"), "channel_id": "ch_2"},
        ],
    }
    await client_b._poll_pending_invites()
    assert sent_b == []


@pytest.mark.asyncio
async def test_throttle_stamps_even_on_dm_failure(store: MessageStore):
    """Mirrors PUF-240's "mark processed even on DM failure"
    invariant — if the transport raised, the operator already saw
    the inbound row in their chat client, so a restart-retry within
    the TTL would be redundant noise."""
    client, _ = _make_client(store)
    client._invites_response = {"invites": [_invite_row("ev_fail")]}

    async def _failing_send_dm(*args, **kwargs):
        raise RuntimeError("simulated DM transport failure")

    client._send_dm = _failing_send_dm  # type: ignore[assignment]

    await client._poll_pending_invites()
    assert await store.was_invite_emitted_within("ev_fail", ttl_ms=_TTL_MS)


@pytest.mark.asyncio
async def test_operator_auto_accept_is_unaffected_by_throttle(store: MessageStore):
    """The throttle gates only the operator-DM emit arm. The auto-
    accept arm (inviter == operator) is uncapped so a restart-retry
    of a not-yet-accepted-by-server invite still tries to drain it."""
    client, sent = _make_client(store, operator_slug="op-1")
    client._invites_response = {
        "invites": [{**_invite_row("ev_auto"), "inviter_slug": "op-1"}],
    }

    accept_calls: list[tuple] = []

    async def _stub_accept_invite(kind, invitation_event_id, space_id, channel_id):
        accept_calls.append((kind, invitation_event_id, space_id, channel_id))

    client._accept_invite = _stub_accept_invite  # type: ignore[assignment]

    await client._poll_pending_invites()
    assert len(accept_calls) == 1
    assert sent == []
    # No throttle stamp on the auto-accept path — leaves the auto-
    # accept arm uncapped across restarts.
    assert not await store.was_invite_emitted_within(
        "ev_auto", ttl_ms=_TTL_MS,
    )
