"""Channel-membership system-message announcements (PUF-317).

When another member joins, leaves, or is removed from a channel this
agent has visibility into, the daemon injects a non-replyable
``[puffo-agent system message]`` envelope into the agent's transcript
so it has read-only context (e.g. stop @-mentioning a member that
just left). Self-actions are deliberately skipped here — the
self-join intro nudge and the self-exit operator DMs cover those.

These tests exercise the two new helpers
(``_maybe_announce_membership_change`` predicate +
``_enqueue_membership_system_message`` injector) plus the
``_handle_event`` wiring that calls them.
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
    """Bare client with just enough state to drive
    ``_handle_event`` through the membership-announce path."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.store = store
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    client._channel_space = {}
    client._space_name_cache = {}
    client._channel_name_cache = {}
    client._space_members = {}

    async def _stub_space_name(space_id: str) -> str:
        return "Team" if space_id == "sp_1" else space_id

    async def _stub_channel_name(*, space_id: str, channel_id: str) -> str:
        return "general" if channel_id == "ch_1" else channel_id

    async def _stub_display_name(slug: str) -> str:
        return {
            "alice-0001": "Alice",
            "bob-0002": "Bob",
            "op-1": "Operator",
        }.get(slug, "")

    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]

    async def _noop_cache(_kind, _event, _payload) -> None:
        return None

    client._maybe_cache_channel_space = _noop_cache  # type: ignore[assignment]
    return client


# ─── _enqueue_membership_system_message direct ────────────────────


@pytest.mark.asyncio
async def test_membership_join_admits_system_priority_envelope():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="alice-0001",
        action="joined",
    )

    assert client._queue.qsize() == 1
    priority, _seq, root_id = await client._queue.get()
    assert priority == PRIORITY_SYSTEM

    entry = client._thread_state[root_id]
    assert len(entry.messages) == 1
    msg = entry.messages[0]
    assert msg["channel_id"] == "ch_1"
    assert msg["channel_name"] == "general"
    assert msg["space_id"] == "sp_1"
    assert msg["sender_slug"] == "system"
    assert msg["is_dm"] is False
    assert msg["envelope_id"].startswith("membership-joined-ch_1-alice-0001-")
    assert "[puffo-agent system message]" in msg["text"]
    assert "Alice" in msg["text"]
    assert "alice-0001" in msg["text"]
    assert "joined" in msg["text"]
    assert "#general" in msg["text"]
    assert "cannot reply" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_membership_left_renders_left_body():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="left",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "left channel #general" in msg["text"]
    assert msg["envelope_id"].startswith("membership-left-ch_1-bob-0002-")
    await store.close()


@pytest.mark.asyncio
async def test_membership_removed_includes_kicker_label():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="removed",
        kicker_slug="alice-0001",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "was removed from channel #general" in msg["text"]
    assert "Alice" in msg["text"]
    assert msg["envelope_id"].startswith(
        "membership-removed-ch_1-bob-0002-"
    )
    await store.close()


@pytest.mark.asyncio
async def test_membership_removed_falls_back_when_kicker_slug_missing():
    """No kicker slug → generic "an operator" label rather than @ ."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="removed",
        kicker_slug="",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "an operator" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_membership_unknown_actor_falls_back_to_slug():
    """Profile-cache miss (display name empty) → @<slug> label."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="charlie-9999",
        action="joined",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "@charlie-9999" in msg["text"]
    # No bold display-name prefix when the name resolved empty.
    assert "**" not in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_membership_envelope_persists_to_messages_db():
    """The synthetic envelope must be queryable via the data-service
    paths the agent uses at runtime so its transcript view is
    consistent."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="alice-0001",
        action="joined",
    )

    _, _, root_id = await client._queue.get()
    envelope = await store.get_message_by_envelope(root_id)
    assert envelope is not None
    assert envelope.channel_id == "ch_1"
    assert envelope.space_id == "sp_1"
    assert envelope.sender_slug == "system"
    assert envelope.thread_root_id == root_id
    assert "joined" in envelope.content

    history = await store.get_channel_history(channel_id="ch_1", limit=10)
    assert len(history) == 1
    assert history[0].envelope_id == root_id
    await store.close()


@pytest.mark.asyncio
async def test_membership_unknown_action_is_noop():
    """Defensive — the dispatcher in ``_handle_event`` only sends
    "joined" / "left" / "removed", but the helper bails cleanly on
    anything else rather than emitting garbage."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="alice-0001",
        action="kicked-out",  # not a valid action
    )

    assert client._queue.qsize() == 0
    assert client._thread_state == {}
    await store.close()


# ─── _maybe_announce_membership_change predicate ──────────────────


@pytest.mark.asyncio
async def test_announce_skipped_when_channel_not_in_visibility_map():
    """Server may fan a leave/remove event to us as a former member
    too. Don't announce on a channel we no longer (or never) had
    visibility into."""
    store = await _make_store()
    client = _make_client(store)
    # _channel_space is empty → no visibility.

    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1"},
    )

    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_announce_skipped_when_actor_is_self():
    """Self-join is covered by the intro nudge; self-exit is covered
    by the operator-DM path. The announce-membership path must NOT
    fire on self-events or we'd double-emit."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "agent-1"},  # self
        {"channel_id": "ch_1"},
    )
    await client._maybe_announce_membership_change(
        "remove_from_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1", "removed_slug": "agent-1"},  # self target
    )
    await client._maybe_announce_membership_change(
        "accept_channel_invite",
        {"signer_slug": "agent-1"},  # self
        {"channel_id": "ch_1"},
    )

    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_announce_unknown_kind_is_noop():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._maybe_announce_membership_change(
        "create_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1"},
    )

    assert client._queue.qsize() == 0
    await store.close()


# ─── _handle_event wiring ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_event_leave_channel_by_other_announces():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_channel",
            "signer_slug": "alice-0001",
            "payload": {"channel_id": "ch_1", "space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "left channel #general" in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_remove_from_channel_by_other_announces_with_kicker():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "remove_from_channel",
            "signer_slug": "alice-0001",  # kicker
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "removed_slug": "bob-0002",
            },
        },
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "Alice" in msg["text"]
    assert "removed" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_accept_channel_invite_by_other_announces_join():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_channel_invite",
            "signer_slug": "alice-0001",  # the joiner
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
            },
        },
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Alice" in msg["text"]
    assert "joined channel #general" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_self_leave_does_not_announce():
    """Self-leave still routes to ``_on_left_channel`` (cache eviction)
    and must NOT fan out as an announcement to its own transcript."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    # Stub _on_left_channel since the real one touches the store.
    on_left_calls: list[str] = []

    async def _stub_on_left(*, channel_id: str) -> None:
        on_left_calls.append(channel_id)

    client._on_left_channel = _stub_on_left  # type: ignore[assignment]

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_channel",
            "signer_slug": "agent-1",
            "payload": {"channel_id": "ch_1", "space_id": "sp_1"},
        },
    )

    # Self-leave path ran.
    assert on_left_calls == ["ch_1"]
    # No announcement in the transcript queue.
    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_self_kicked_does_not_announce():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    on_kicked_calls: list[dict] = []

    async def _stub_on_kicked(
        *, channel_id: str, space_id: str, kicker_slug: str,
    ) -> None:
        on_kicked_calls.append(
            {"channel_id": channel_id, "kicker_slug": kicker_slug},
        )

    client._on_kicked_from_channel = _stub_on_kicked  # type: ignore[assignment]

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "remove_from_channel",
            "signer_slug": "alice-0001",
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "removed_slug": "agent-1",  # self
            },
        },
    )

    assert len(on_kicked_calls) == 1
    assert on_kicked_calls[0]["kicker_slug"] == "alice-0001"
    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_other_leave_on_unknown_channel_is_silent():
    """Channel not in our visibility map → server fan-out noise; no
    announcement."""
    store = await _make_store()
    client = _make_client(store)
    # _channel_space empty; we're not a member of ch_unknown.

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_channel",
            "signer_slug": "alice-0001",
            "payload": {"channel_id": "ch_unknown", "space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 0
    await store.close()
