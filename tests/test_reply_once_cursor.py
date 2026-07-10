"""T27 regression: the reply-once / "agent replies once then silent" bug.

The persisted per-thread dedup cursor (``thread_processing_state``)
used to reject any inbound message with ``sent_at <= watermark``. But
``sent_at`` is epoch-milliseconds and NOT unique — a programmatic
burst (agent→agent traffic; the server stamps consecutive bridge
``Send`` frames with its own ``now_ms``) can put two DISTINCT
envelopes on the same thread root in the same millisecond. The second,
genuinely-new message was then dropped as a "duplicate" and the agent
never took a second turn.

The fix keys equality on the envelope_id: the cursor now records the
envelope_ids processed AT the watermark ``sent_at``, and the inbound
gate rejects only ``sent_at < watermark`` or (``sent_at == watermark``
AND the envelope_id is recorded). Both properties are pinned here:

  (i)  a NEW same-``sent_at`` message on an active root IS admitted
       and dispatched (DM case + channel-thread case);
  (ii) a genuinely-redelivered already-processed envelope (same
       envelope_id + sent_at, e.g. /messages/pending replay after a
       daemon restart) is STILL dropped — including across a
       simulated restart (fresh client, same sqlite store).

These tests drive the REAL inbound tail (``_handle_plaintext_payload``
→ cursor gate → ``_admit_thread_message`` → ``_consume_queue``), not
the queue internals, so the gate itself is under test.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import puffo_agent.agent.puffo_core_client as pcc_mod
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore
from puffo_agent.crypto.message import MessagePayload

T = 1_700_000_000_000  # one fixed millisecond — the collision under test


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    # The consumer sleeps random.uniform(0, 1.5) before dispatch; zero
    # it so batch-callback tests don't wait wall-clock seconds.
    monkeypatch.setattr(pcc_mod.random, "uniform", lambda a, b: 0.0)


async def _make_client(tmp_path, db: str, *, store: MessageStore | None = None):
    """A native-config message client with offline HTTP so every
    enrichment helper degrades to ids-for-names. Pass ``store`` to
    share one sqlite DB across two clients (restart simulation)."""
    ks = KeyStore(str(tmp_path / f"keys-{db}"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, "bot-0001")
    store = store or MessageStore(str(tmp_path / db))
    client = PuffoCoreMessageClient(
        slug="bot-0001",
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=store,
    )

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    await client.store.open()
    return client


def _dm(envelope_id: str, *, sent_at: int, thread_root_id: str | None = None):
    return MessagePayload(
        payload_type="puffo.message", version=1,
        envelope_id=envelope_id, envelope_kind="dm",
        sender_slug="alice-0001", sender_subkey_id="", sent_at=sent_at,
        message_nonce="", content_type="text/plain",
        content=f"dm body {envelope_id}", is_visible_to_human=True,
        recipient_slug="bot-0001", thread_root_id=thread_root_id,
    )


def _channel_msg(
    envelope_id: str, *, sent_at: int, thread_root_id: str | None = None,
):
    return MessagePayload(
        payload_type="puffo.message", version=1,
        envelope_id=envelope_id, envelope_kind="channel",
        sender_slug="alice-0001", sender_subkey_id="", sent_at=sent_at,
        message_nonce="", content_type="text/plain",
        content=f"channel body {envelope_id}", is_visible_to_human=True,
        space_id="sp_1", channel_id="ch_1", thread_root_id=thread_root_id,
    )


async def _consume_one_batch(client, timeout: float = 2.0):
    """Run the consumer until exactly one batch dispatches; return
    ``(root_id, [envelope_ids])``. ``done`` keys on ``on_turn_success``
    — which the consumer fires AFTER persisting the cursor — so the
    cancel below can't race the ``mark_thread_processed`` write."""
    received: list[tuple[str, list[str]]] = []
    done = asyncio.Event()

    async def on_batch(root_id, batch, channel_meta):
        received.append((root_id, [m["envelope_id"] for m in batch]))

    async def on_turn_success(root_id, batch, channel_meta):
        done.set()

    task = asyncio.create_task(
        client._consume_queue(on_batch, None, None, on_turn_success),
    )
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert len(received) == 1
    return received[0]


def _queued_envelope_ids(client, root_id: str) -> list[str]:
    entry = client._thread_state.get(root_id)
    if entry is None or not entry.in_queue:
        return []
    return [m["envelope_id"] for m in entry.messages]


# ─── (i) NEW same-sent_at message on an active root gets a 2nd turn ──


@pytest.mark.asyncio
async def test_dm_same_sent_at_new_envelope_gets_second_turn(tmp_path):
    """DM case: message A (sent_at=T) is processed; message B — a
    DIFFERENT envelope on the same root (threaded DM reply), same
    sent_at=T — must be admitted and dispatched as a second turn.
    Before the fix, B was cursor-rejected and the agent went silent."""
    client = await _make_client(tmp_path, "dm_second_turn.db")
    try:
        await client._handle_plaintext_payload(_dm("env_dm_a", sent_at=T))
        root_id, first = await _consume_one_batch(client)
        assert root_id == "env_dm_a"
        assert first == ["env_dm_a"]

        # B arrives on the SAME root (thread_root_id → A, which is in
        # the local store, so admit-time validation keeps it) in the
        # SAME ms.
        await client._handle_plaintext_payload(
            _dm("env_dm_b", sent_at=T, thread_root_id="env_dm_a"),
        )
        assert _queued_envelope_ids(client, "env_dm_a") == ["env_dm_b"], (
            "new same-sent_at DM must be admitted, not cursor-rejected"
        )
        root_id_2, second = await _consume_one_batch(client)
        assert root_id_2 == "env_dm_a"
        assert second == ["env_dm_b"], "agent must take a 2nd turn for the DM"
    finally:
        # Always close: a lingering aiosqlite thread (non-daemon)
        # blocks interpreter exit and hangs the whole pytest run.
        await client.store.close()


@pytest.mark.asyncio
async def test_channel_thread_same_sent_at_new_envelope_gets_second_turn(tmp_path):
    """Thread case: same collision on a channel thread root."""
    client = await _make_client(tmp_path, "thread_second_turn.db")
    try:
        await client._handle_plaintext_payload(
            _channel_msg("env_th_a", sent_at=T),
        )
        root_id, first = await _consume_one_batch(client)
        assert root_id == "env_th_a"
        assert first == ["env_th_a"]

        await client._handle_plaintext_payload(
            _channel_msg("env_th_b", sent_at=T, thread_root_id="env_th_a"),
        )
        assert _queued_envelope_ids(client, "env_th_a") == ["env_th_b"], (
            "new same-sent_at thread reply must be admitted, "
            "not cursor-rejected"
        )
        root_id_2, second = await _consume_one_batch(client)
        assert root_id_2 == "env_th_a"
        assert second == ["env_th_b"], (
            "agent must take a 2nd turn for the thread"
        )
    finally:
        await client.store.close()


# ─── (ii) redelivered already-processed envelopes are STILL dropped ──


@pytest.mark.asyncio
async def test_redelivered_processed_envelopes_still_dropped_across_restart(tmp_path):
    """Cross-restart replay: after A and B (same sent_at) were both
    processed, a fresh client over the SAME sqlite store (fresh
    in-memory queue/thread state, i.e. a daemon restart) must drop the
    server's /messages/pending redelivery of BOTH — the cursor keeps
    its dedup property at the watermark via the recorded ids."""
    store = MessageStore(str(tmp_path / "restart_dedup.db"))
    try:
        client = await _make_client(tmp_path, "restart_dedup.db", store=store)

        await client._handle_plaintext_payload(_dm("env_r_a", sent_at=T))
        await _consume_one_batch(client)
        await client._handle_plaintext_payload(
            _dm("env_r_b", sent_at=T, thread_root_id="env_r_a"),
        )
        await _consume_one_batch(client)
        assert await store.get_thread_cursor("env_r_a") == (
            T, {"env_r_a", "env_r_b"},
        )

        # "Restart": new client, same store, empty in-memory state.
        client2 = await _make_client(
            tmp_path, "restart_dedup.db", store=store,
        )
        await client2._handle_plaintext_payload(_dm("env_r_a", sent_at=T))
        await client2._handle_plaintext_payload(
            _dm("env_r_b", sent_at=T, thread_root_id="env_r_a"),
        )
        assert client2._queue.qsize() == 0, (
            "redelivered envelopes must not re-queue"
        )
        assert client2._thread_state == {}

        # An older message on the same root (below the watermark) is
        # likewise still dropped.
        await client2._handle_plaintext_payload(
            _dm("env_r_old", sent_at=T - 1, thread_root_id="env_r_a"),
        )
        assert client2._queue.qsize() == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_redelivery_dropped_same_session_after_dispatch(tmp_path):
    """Same-session replay (WS reconnect + fetch_pending): once B's
    batch has dispatched and the cursor covers it, a redelivered B is
    dropped by the durable gate even though dispatching_ids was
    already cleared."""
    client = await _make_client(tmp_path, "session_dedup.db")
    try:
        await client._handle_plaintext_payload(
            _channel_msg("env_s_a", sent_at=T),
        )
        await _consume_one_batch(client)
        await client._handle_plaintext_payload(
            _channel_msg("env_s_b", sent_at=T, thread_root_id="env_s_a"),
        )
        await _consume_one_batch(client)

        await client._handle_plaintext_payload(
            _channel_msg("env_s_b", sent_at=T, thread_root_id="env_s_a"),
        )
        assert _queued_envelope_ids(client, "env_s_a") == []
        assert client._queue.qsize() == 0
    finally:
        await client.store.close()


# ─── store-level semantics ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_thread_processed_watermark_id_semantics(tmp_path):
    store = MessageStore(str(tmp_path / "cursor_semantics.db"))
    await store.open()
    try:
        # Fresh root: (0, empty).
        assert await store.get_thread_cursor("root") == (0, set())

        # First advance records the watermark ties.
        await store.mark_thread_processed("root", 1000, ["e1"])
        assert await store.get_thread_cursor("root") == (1000, {"e1"})

        # Equal sent_at unions the ids (a later batch ending on the
        # same ms).
        await store.mark_thread_processed("root", 1000, ["e2"])
        assert await store.get_thread_cursor("root") == (1000, {"e1", "e2"})

        # Regress is a no-op (ids untouched too).
        await store.mark_thread_processed("root", 500, ["e_old"])
        assert await store.get_thread_cursor("root") == (1000, {"e1", "e2"})

        # A higher watermark replaces both — stale ties don't
        # accumulate forever.
        await store.mark_thread_processed("root", 2000, ["e3"])
        assert await store.get_thread_cursor("root") == (2000, {"e3"})

        # Legacy call shape (no ids) still advances the watermark.
        await store.mark_thread_processed("root", 3000)
        assert await store.get_thread_cursor("root") == (3000, set())
        assert await store.get_last_processed_sent_at("root") == 3000
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_schema_migration_adds_envelope_ids_column(tmp_path):
    """A DB created by the OLD schema (no ``last_processed_envelope_ids``
    column, one populated cursor row) opens cleanly: the column is
    added, the legacy row reads as an empty id set (fail-open at the
    watermark), and new writes union/replace as normal."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE thread_processing_state ("
            " root_id TEXT PRIMARY KEY,"
            " last_processed_sent_at INTEGER NOT NULL)"
        )
        await db.execute(
            "INSERT INTO thread_processing_state VALUES ('root_legacy', 500)"
        )
        await db.commit()

    store = MessageStore(str(db_path))
    await store.open()
    try:
        assert await store.get_thread_cursor("root_legacy") == (500, set())
        # Equal-watermark write on the migrated row unions into the
        # (empty) set.
        await store.mark_thread_processed("root_legacy", 500, ["e_new"])
        assert await store.get_thread_cursor("root_legacy") == (
            500, {"e_new"},
        )
        # Migration is idempotent across reopen; data survives.
        await store.close()
        await store.open()
        assert await store.get_thread_cursor("root_legacy") == (
            500, {"e_new"},
        )
        # And the persisted column really is JSON.
        db2 = await store._ensure_db()
        async with db2.execute(
            "SELECT last_processed_envelope_ids FROM thread_processing_state "
            "WHERE root_id = 'root_legacy'"
        ) as cursor:
            row = await cursor.fetchone()
        assert json.loads(row[0]) == ["e_new"]
    finally:
        await store.close()
