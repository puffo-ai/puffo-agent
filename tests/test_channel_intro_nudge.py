"""Self-introduction nudge fired after a channel-invite accept.

When the agent accepts an ``invite_to_channel``, the daemon enqueues a
synthetic ``[puffo-agent system message]`` envelope into the agent's
thread queue so it posts a short intro using its normal
``mcp__puffo__send_message`` path. The nudge is dedup-ed per channel
via the ``channel_intro_prompted`` sqlite table so a daemon restart or
a server-side invite redelivery can't fire a second intro.

These tests exercise ``_enqueue_channel_intro_nudge``,
``_find_public_general_channel`` and the ``MessageStore`` helpers
directly. The wiring inside ``_accept_invite`` is a thin try/except
wrapper on top — covered by manual smoke tests.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import (
    PRIORITY_SYSTEM,
    PuffoCoreMessageClient,
)


async def _make_store() -> MessageStore:
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    return store


def _make_client(store: MessageStore) -> PuffoCoreMessageClient:
    """Bare client with just enough state to exercise the intro path.
    Mirrors ``test_thread_queue._make_client_for_queue`` and stubs the
    HTTP-backed name resolvers so no network is touched."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.store = store
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    # ``_find_public_general_channel`` warms this cache from the
    # /channels response so the immediately-following
    # ``_resolve_channel_name`` inside the intro nudge becomes a hit.
    client._channel_name_cache = {}

    async def _stub_space_name(space_id: str) -> str:
        return "Team" if space_id == "sp_1" else space_id

    async def _stub_channel_name(space_id: str, channel_id: str) -> str:
        return "general" if channel_id == "ch_1" else channel_id

    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    return client


def _instant_sleep_monkeypatch(monkeypatch) -> None:
    """Skip the real backoff sleep so the suite stays fast."""
    import puffo_agent.agent.puffo_core_client as _client_mod

    async def _instant_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(_client_mod.asyncio, "sleep", _instant_sleep)


# ─── MessageStore helpers ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_intro_helpers_idempotent():
    store = await _make_store()
    assert await store.has_channel_intro_been_prompted("ch_1") is False

    await store.mark_channel_intro_prompted("ch_1")
    assert await store.has_channel_intro_been_prompted("ch_1") is True

    # Second mark is a no-op (ON CONFLICT DO NOTHING). The state stays
    # truthy and the underlying row count must remain 1.
    await store.mark_channel_intro_prompted("ch_1")
    assert await store.has_channel_intro_been_prompted("ch_1") is True

    db = await store._ensure_db()
    async with db.execute(
        "SELECT COUNT(*) FROM channel_intro_prompted WHERE channel_id = 'ch_1'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1

    # Empty channel_id is silently ignored on both reads and writes.
    assert await store.has_channel_intro_been_prompted("") is False
    await store.mark_channel_intro_prompted("")
    await store.close()


# ─── _enqueue_channel_intro_nudge ─────────────────────────────────


@pytest.mark.asyncio
async def test_intro_nudge_admits_one_system_priority_envelope():
    store = await _make_store()
    client = _make_client(store)

    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )

    # Queue holds exactly one tuple with PRIORITY_SYSTEM.
    assert client._queue.qsize() == 1
    priority, _seq, root_id = await client._queue.get()
    assert priority == PRIORITY_SYSTEM

    # State has a single ThreadEntry keyed on the synthetic envelope_id
    # (root_id == envelope_id since this is a top-level post).
    assert root_id in client._thread_state
    entry = client._thread_state[root_id]
    assert len(entry.messages) == 1
    msg = entry.messages[0]

    assert msg["channel_id"] == "ch_1"
    assert msg["channel_name"] == "general"
    assert msg["space_id"] == "sp_1"
    assert msg["space_name"] == "Team"
    assert msg["is_dm"] is False
    assert msg["attachments"] == []
    assert msg["envelope_id"].startswith("intro-prompt-ch_1-")
    assert msg["envelope_id"] == root_id
    assert "[puffo-agent system message]" in msg["text"]
    assert "ch_1" in msg["text"]
    assert "general" in msg["text"]
    assert "send_message" in msg["text"]

    assert await store.has_channel_intro_been_prompted("ch_1") is True
    await store.close()


@pytest.mark.asyncio
async def test_intro_nudge_persists_envelope_to_messages_db():
    """The synthetic envelope must be queryable via the data-service
    paths the agent uses at runtime — ``get_channel_history`` /
    ``get_message_by_envelope`` / ``lookup_channel_space`` — so the
    agent's view of the channel is consistent with what it just
    received in its turn prompt. Without persistence the agent
    would see the intro in the user-block but a follow-up
    ``get_channel_history`` would return an empty list (or a list
    that omits the intro), and ``send_message(root_id=<intro id>)``
    would surface as a broken thread reference."""
    store = await _make_store()
    client = _make_client(store)

    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )

    # The envelope is queryable by its id.
    _, _, root_id = await client._queue.get()
    envelope = await store.get_message_by_envelope(root_id)
    assert envelope is not None
    assert envelope.channel_id == "ch_1"
    assert envelope.space_id == "sp_1"
    assert envelope.sender_slug == "system"
    assert envelope.thread_root_id == root_id
    assert "[puffo-agent system message]" in envelope.content

    # And it shows up in the channel-history view that the
    # agent's MCP tooling pulls from.
    history = await store.get_channel_history(channel_id="ch_1", limit=10)
    assert len(history) == 1
    assert history[0].envelope_id == root_id

    # Bonus: ``lookup_channel_space`` learns the mapping off the
    # persisted envelope so ``send_message`` resolves the right
    # space without an extra round-trip.
    assert await store.lookup_channel_space("ch_1") == "sp_1"

    await store.close()


@pytest.mark.asyncio
async def test_intro_nudge_skipped_when_already_prompted():
    store = await _make_store()
    client = _make_client(store)

    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )
    assert client._queue.qsize() == 1

    # Second call (simulating a redelivered invite or restart-time
    # re-accept) must be a no-op — same channel, table already marked.
    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )
    assert client._queue.qsize() == 1
    assert len(client._thread_state) == 1
    await store.close()


@pytest.mark.asyncio
async def test_intro_nudge_distinct_channels_each_get_one():
    """Two separate channel invites = two separate nudges. Dedup is
    per channel, not global."""
    store = await _make_store()
    client = _make_client(store)

    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )
    await client._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_2",
    )

    assert client._queue.qsize() == 2
    assert await store.has_channel_intro_been_prompted("ch_1") is True
    assert await store.has_channel_intro_been_prompted("ch_2") is True
    await store.close()


# ─── _find_public_general_channel (against /spaces/<id>/channels) ─


def _channels_response(*entries: dict) -> dict:
    """Wrap channel rows in the shape ``GET /spaces/<id>/channels``
    returns (matches ``server::space_config::ListChannelsResponse``)."""
    return {"channels": list(entries)}


@pytest.mark.asyncio
async def test_find_public_general_channel_picks_is_public_true(monkeypatch):
    """Returns the first row with ``is_public=true``. Server already
    filters the list by membership, so anything we see is reachable —
    we just need to pick General."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, path: str) -> dict:
            assert path == "/spaces/sp_1/channels"
            return _channels_response(
                {
                    "channel_id": "ch_private",
                    "name": "Random",
                    "is_public": False,
                },
                {
                    "channel_id": "ch_general",
                    "name": "General",
                    "is_public": True,
                },
            )

    client.http = _StubHttp()
    cid = await client._find_public_general_channel("sp_1")
    assert cid == "ch_general"
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_persists_channel_space_map(monkeypatch):
    """Every channel returned by /channels lands in the
    ``channel_space_map`` table so ``lookup_channel_space`` can
    resolve it BEFORE the first inbound message — the MCP
    ``send_message`` tool depends on this for the intro nudge."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, _path: str) -> dict:
            return _channels_response(
                {
                    "channel_id": "ch_random",
                    "name": "Random",
                    "is_public": False,
                },
                {
                    "channel_id": "ch_general",
                    "name": "General",
                    "is_public": True,
                },
            )

    client.http = _StubHttp()
    await client._find_public_general_channel("sp_1")

    # Both channels are now resolvable by lookup_channel_space —
    # private ones too, since the agent might still want to send
    # there later.
    assert await store.lookup_channel_space("ch_random") == "sp_1"
    assert await store.lookup_channel_space("ch_general") == "sp_1"
    await store.close()


@pytest.mark.asyncio
async def test_lookup_channel_space_prefers_map_over_messages_inference():
    """Explicit map wins over the /messages-table fallback. Same
    channel could in principle show up under two space_ids if the
    server emits conflicting envelopes; the explicit map is
    authoritative."""
    store = await _make_store()

    await store.mark_channel_space("ch_1", "sp_authoritative")
    # Plant a "wrong" message-level signal too.
    await store.store({
        "envelope_id": "env_1",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_1",
        "space_id": "sp_stale",
        "content_type": "text/plain",
        "content": "hi",
        "sent_at": 1,
    })

    assert await store.lookup_channel_space("ch_1") == "sp_authoritative"
    await store.close()


@pytest.mark.asyncio
async def test_lookup_channel_space_falls_back_to_messages_when_no_map_row():
    """When ``channel_space_map`` has no entry, the historical
    /messages-table inference still works — keeps steady-state
    behaviour for channels we learned about via inbound traffic."""
    store = await _make_store()
    await store.store({
        "envelope_id": "env_1",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_inferred",
        "space_id": "sp_via_msg",
        "content_type": "text/plain",
        "content": "hi",
        "sent_at": 1,
    })

    assert await store.lookup_channel_space("ch_inferred") == "sp_via_msg"
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_warms_channel_name_cache(monkeypatch):
    """The /channels response we already pay for doubles as a cache
    warmup so the ``_resolve_channel_name`` inside the intro nudge
    doesn't have to round-trip again seconds later."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, _path: str) -> dict:
            return _channels_response(
                {
                    "channel_id": "ch_random",
                    "name": "Random",
                    "is_public": False,
                },
                {
                    "channel_id": "ch_general",
                    "name": "General",
                    "is_public": True,
                },
            )

    client.http = _StubHttp()
    await client._find_public_general_channel("sp_1")
    assert client._channel_name_cache == {
        "ch_random": "Random",
        "ch_general": "General",
    }
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_returns_empty_when_none(monkeypatch):
    """No public channel in the response → empty string. Caller
    treats this as 'skip the intro' (no obvious landing channel)."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, _path: str) -> dict:
            return _channels_response({
                "channel_id": "ch_private",
                "name": "Random",
                "is_public": False,
            })

    client.http = _StubHttp()
    cid = await client._find_public_general_channel("sp_1")
    assert cid == ""
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_retries_when_endpoint_returns_string(monkeypatch):
    """Accept POST → channels GET is a tight race: the server has the
    accept event applied but the ``channel_memberships`` row that
    gates the endpoint may not be committed yet. In that window the
    endpoint returns the SPA fallback (decoded as ``str``); we sleep
    and retry. Third call wins → General resolved."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    calls: list[int] = []

    class _FlakyHttp:
        async def get(self, _path: str):
            calls.append(1)
            if len(calls) < 3:
                return ""  # not-a-member-yet stand-in
            return _channels_response({
                "channel_id": "ch_general",
                "name": "General",
                "is_public": True,
            })

    client.http = _FlakyHttp()
    cid = await client._find_public_general_channel("sp_1")
    assert cid == "ch_general"
    assert len(calls) == 3
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_gives_up_after_all_retries(monkeypatch):
    """Endpoint stays unhappy across all attempts → return ``""``, no
    exception raised. The accept itself isn't blocked."""
    _instant_sleep_monkeypatch(monkeypatch)
    store = await _make_store()
    client = _make_client(store)

    class _AlwaysString:
        async def get(self, _path: str):
            return ""

    client.http = _AlwaysString()
    cid = await client._find_public_general_channel("sp_1")
    assert cid == ""
    await store.close()


@pytest.mark.asyncio
async def test_intro_nudge_survives_simulated_restart():
    """Dedup is persistent: a fresh client built on the same db path
    must still treat the channel as already-prompted."""
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "messages.db")

    store_1 = MessageStore(db_path)
    await store_1.open()
    client_1 = _make_client(store_1)
    client_1.store = store_1
    await client_1._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )
    assert client_1._queue.qsize() == 1
    await store_1.close()

    # Simulated restart — new MessageStore over the same file.
    store_2 = MessageStore(db_path)
    await store_2.open()
    client_2 = _make_client(store_2)
    client_2.store = store_2
    await client_2._enqueue_channel_intro_nudge(
        space_id="sp_1", channel_id="ch_1",
    )
    assert client_2._queue.qsize() == 0
    await store_2.close()
