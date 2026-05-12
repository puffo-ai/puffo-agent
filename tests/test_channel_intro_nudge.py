"""Self-introduction nudge fired after a channel-invite accept.

When the agent accepts an ``invite_to_channel``, the daemon enqueues a
synthetic ``[puffo-agent system message]`` envelope into the agent's
thread queue so it posts a short intro using its normal
``mcp__puffo__send_message`` path. The nudge is dedup-ed per channel
via the ``channel_intro_prompted`` sqlite table so a daemon restart or
a server-side invite redelivery can't fire a second intro.

These tests exercise ``_enqueue_channel_intro_nudge`` and the
``MessageStore`` helpers directly. The wiring inside ``_accept_invite``
is a thin try/except wrapper on top — covered by manual smoke tests.
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

    async def _stub_space_name(space_id: str) -> str:
        return "Team" if space_id == "sp_1" else space_id

    async def _stub_channel_name(space_id: str, channel_id: str) -> str:
        return "general" if channel_id == "ch_1" else channel_id

    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    return client


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

    # Shape sanity: channel ids resolved, prompt prefixed correctly,
    # not a DM, no attachments.
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

    # Dedup row landed.
    assert await store.has_channel_intro_been_prompted("ch_1") is True
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
    assert client._queue.qsize() == 1  # still just the first one
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


@pytest.mark.asyncio
async def test_find_public_general_channel_picks_is_public_true():
    """``_find_public_general_channel`` walks the space's events,
    returns the first ``create_channel`` event with ``is_public=true``.
    Public-flag check is the canonical signal — server emits a
    synthetic CreateChannel with ``is_public=true`` for every new
    space's General; user-created channels default false."""
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, path: str) -> dict:
            assert path.startswith("/spaces/sp_1/events")
            return {
                "events": [
                    {
                        "kind": "create_channel",
                        "payload": {
                            "channel_id": "ch_private",
                            "name": "Random",
                            "is_public": False,
                        },
                    },
                    {
                        "kind": "create_channel",
                        "payload": {
                            "channel_id": "ch_general",
                            "name": "General",
                            "is_public": True,
                        },
                    },
                ],
                "has_more": False,
            }

    client.http = _StubHttp()
    cid = await client._find_public_general_channel("sp_1")
    assert cid == "ch_general"
    await store.close()


@pytest.mark.asyncio
async def test_find_public_general_channel_returns_empty_when_none():
    """No public channel in the space → empty string. Caller treats
    this as 'skip the intro' (no obvious landing channel)."""
    store = await _make_store()
    client = _make_client(store)

    class _StubHttp:
        async def get(self, _path: str) -> dict:
            return {
                "events": [
                    {
                        "kind": "create_channel",
                        "payload": {
                            "channel_id": "ch_private",
                            "name": "Random",
                            "is_public": False,
                        },
                    },
                ],
                "has_more": False,
            }

    client.http = _StubHttp()
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
    assert client_2._queue.qsize() == 0  # gate held across restart
    await store_2.close()
