"""Channel-membership system-message announcements (other-actor only)."""

from __future__ import annotations

import asyncio
import logging
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
    """Bare client wired enough to drive the membership-announce path."""
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
    client._processed_membership_event_ids = set()

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
    client._inviter_by_invitation_event_id = {}
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
    assert "**" not in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_membership_envelope_persists_to_messages_db():
    """Envelope queryable via get_message_by_envelope + get_channel_history."""
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
    """Helper bails cleanly on an action the dispatcher wouldn't send."""
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
    """Don't announce on a channel we no longer (or never) had visibility into."""
    store = await _make_store()
    client = _make_client(store)

    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1"},
    )

    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_announce_skipped_when_actor_is_self():
    """Self-events are owned by other paths (intro nudge / operator DM)."""
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
    """Self-leave routes to _on_left_channel, not the announce path."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

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

    assert on_left_calls == ["ch_1"]
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
    """Unknown channel → server fan-out noise; no announce."""
    store = await _make_store()
    client = _make_client(store)

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


# ─── per-event-id idempotency (reconnect-replay dedup) ────────────


@pytest.mark.asyncio
async def test_announce_dedups_on_duplicate_event_id():
    """Reconnect-replay of the same signed event_id is a no-op."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    event = {
        "kind": "leave_channel",
        "signer_slug": "alice-0001",
        "event_id": "ev_replay_test",
        "payload": {"channel_id": "ch_1", "space_id": "sp_1"},
    }

    await client._handle_event(scope="sp_1", event=event)
    await client._handle_event(scope="sp_1", event=event)

    assert client._queue.qsize() == 1
    assert "ev_replay_test" in client._processed_membership_event_ids
    await store.close()


@pytest.mark.asyncio
async def test_announce_uses_event_id_in_envelope_id_when_present():
    """Deterministic envelope_id so sqlite INSERT OR IGNORE catches replay across restart."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_channel",
            "signer_slug": "alice-0001",
            "event_id": "ev_abc123",
            "payload": {"channel_id": "ch_1", "space_id": "sp_1"},
        },
    )

    _, _, root_id = await client._queue.get()
    assert root_id == "membership-left-ch_1-alice-0001-ev_abc123"
    await store.close()


@pytest.mark.asyncio
async def test_announce_falls_back_to_timestamp_when_event_id_missing():
    """No event_id → ms-timestamp suffix, never empty."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1", "space_id": "sp_1"},
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    assert root_id.startswith("membership-left-ch_1-alice-0001-")
    assert not root_id.endswith("-")
    await store.close()


@pytest.mark.asyncio
async def test_announce_skips_dedup_when_event_id_empty():
    """Empty event_id must not poison the dedup set."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "alice-0001"},
        {"channel_id": "ch_1", "space_id": "sp_1"},
    )
    await client._maybe_announce_membership_change(
        "leave_channel",
        {"signer_slug": "bob-0002"},
        {"channel_id": "ch_1", "space_id": "sp_1"},
    )

    assert client._queue.qsize() == 2
    assert client._processed_membership_event_ids == set()
    await store.close()


# ─── persistence-failure mirror ───────────────────────────────────


@pytest.mark.asyncio
async def test_persist_failure_still_delivers_in_memory_envelope(
    monkeypatch, caplog,
):
    """sqlite raising must not block in-memory delivery; warn instead."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"
    client._log = logging.getLogger("test_persist_failure")

    async def _explode(_payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(client.store, "store", _explode)

    with caplog.at_level(logging.WARNING, logger="test_persist_failure"):
        await client._enqueue_membership_system_message(
            channel_id="ch_1",
            actor_slug="alice-0001",
            action="joined",
        )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    assert "joined" in client._thread_state[root_id].messages[0]["text"]

    assert any(
        "membership system-message" in rec.message
        and "failed to persist" in rec.message
        for rec in caplog.records
    )
    await store.close()


# ─── inviter citation in joined body ─────────────


@pytest.mark.asyncio
async def test_joined_body_cites_inviter_when_supplied():
    """inviter_slug supplied → '(invited by X)' suffix."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="joined",
        inviter_slug="alice-0001",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "joined channel #general" in msg["text"]
    assert "(invited by " in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_joined_body_omits_inviter_when_empty():
    """Empty inviter_slug → no '(invited by …)' fragment at all."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="joined",
        inviter_slug="",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "joined channel #general" in msg["text"]
    assert "invited by" not in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_accept_channel_invite_with_original_invite_cites_inviter():
    """Server-auto-accept payload path: original_invite.signer_slug
    must be lifted into the rendered body."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_channel_invite",
            "signer_slug": "bob-0002",  # the joiner
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "original_invite": {"signer_slug": "alice-0001"},
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "joined channel #general" in msg["text"]
    assert "(invited by " in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


# ─── space-membership announce path ─────────────


@pytest.mark.asyncio
async def test_space_membership_joined_renders_joined_space_body():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="alice-0001",
        action="joined_space",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert msg["envelope_id"].startswith(
        "membership-joined_space-ch_1-alice-0001-"
    )
    assert "Alice" in msg["text"]
    assert "joined space" in msg["text"]
    assert "**Team**" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_left_renders_left_space_body():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="left_space",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "left space" in msg["text"]
    assert "**Team**" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_removed_includes_kicker_and_space():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._enqueue_membership_system_message(
        channel_id="ch_1",
        actor_slug="bob-0002",
        action="removed_from_space",
        kicker_slug="alice-0001",
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "was removed from space" in msg["text"]
    assert "**Team**" in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_leave_space_by_other_announces_into_first_channel():
    """Space cascade-leave has no per-channel event; the space-announce path picks a channel."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "payload": {"space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert msg["channel_id"] == "ch_1"
    assert "Alice" in msg["text"]
    assert "left space" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_remove_from_space_by_other_announces_with_kicker():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "remove_from_space",
            "signer_slug": "alice-0001",  # kicker
            "payload": {
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
    assert "removed from space" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_accept_space_invite_by_other_announces_join():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_space_invite",
            "signer_slug": "alice-0001",
            "payload": {"space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 1
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Alice" in msg["text"]
    assert "joined space" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_handle_event_accept_space_invite_cites_inviter_when_present():
    """original_invite.signer_slug → '(invited by X)' suffix."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_space_invite",
            "signer_slug": "bob-0002",
            "payload": {
                "space_id": "sp_1",
                "original_invite": {"signer_slug": "alice-0001"},
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "(invited by " in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_skips_when_no_channel_visibility():
    """No cached channel + no http stub (→ exception → caught) → skip."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_other"] = "sp_2"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "payload": {"space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_skips_self_actor():
    """Self space events are owned by other paths (_on_left_space etc.)."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._maybe_announce_space_membership_change(
        "leave_space",
        {"signer_slug": "agent-1"},
        {"space_id": "sp_1"},
    )
    await client._maybe_announce_space_membership_change(
        "remove_from_space",
        {"signer_slug": "alice-0001"},
        {"space_id": "sp_1", "removed_slug": "agent-1"},
    )
    await client._maybe_announce_space_membership_change(
        "accept_space_invite",
        {"signer_slug": "agent-1"},
        {"space_id": "sp_1"},
    )

    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_falls_back_to_lex_first_when_no_general():
    """No #general → lex-first channel_id (stable, dedup-friendly)."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_z"] = "sp_1"
    client._channel_space["ch_a"] = "sp_1"
    client._channel_space["ch_m"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "event_id": "ev_pick",
            "payload": {"space_id": "sp_1"},
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert msg["channel_id"] == "ch_a"
    assert root_id == "membership-left_space-ch_a-alice-0001-ev_pick"
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_prefers_general_channel_when_present():
    """Space-scope events land in #general (matches the human UI)."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_zulu"] = "sp_1"
    client._channel_space["ch_general"] = "sp_1"
    client._channel_space["ch_alpha"] = "sp_1"

    async def _stub_channel_name(*, space_id: str, channel_id: str) -> str:
        return {
            "ch_general": "General",
            "ch_alpha": "alpha",
            "ch_zulu": "zulu",
        }.get(channel_id, channel_id)

    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "event_id": "ev_general",
            "payload": {"space_id": "sp_1"},
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert msg["channel_id"] == "ch_general"
    assert root_id == "membership-left_space-ch_general-alice-0001-ev_general"
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_lazy_fetches_channels_when_cache_empty():
    """Cascade-joined agents have an empty ``_channel_space`` for the
    space; the announce path must fall back to GET /spaces/<id>/channels
    and warm the cache for next time."""
    store = await _make_store()
    client = _make_client(store)
    # _channel_space is empty for sp_1 — simulates the cascade-joined case.

    fetched: list[str] = []

    class _StubHttp:
        async def get(self, path: str):
            fetched.append(path)
            return {
                "channels": [
                    {"channel_id": "ch_zulu", "name": "zulu"},
                    {"channel_id": "ch_general", "name": "General"},
                    {"channel_id": "ch_alpha", "name": "alpha"},
                ],
            }

    client.http = _StubHttp()  # type: ignore[assignment]

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "event_id": "ev_cascade",
            "payload": {"space_id": "sp_1"},
        },
    )

    assert fetched == ["/spaces/sp_1/channels"]
    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert msg["channel_id"] == "ch_general"
    # Cache warmed for next time so we don't refetch on every space event.
    assert client._channel_space == {
        "ch_zulu": "sp_1",
        "ch_general": "sp_1",
        "ch_alpha": "sp_1",
    }
    assert client._channel_name_cache["ch_general"] == "General"
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_silent_skip_when_http_fetch_fails():
    """HTTP failure on the lazy-fetch fallback is silent — no announce,
    no crash. Matches the prior 'no visibility → skip' contract."""
    store = await _make_store()
    client = _make_client(store)

    class _ExplodingHttp:
        async def get(self, path: str):
            raise RuntimeError("server unreachable")

    client.http = _ExplodingHttp()  # type: ignore[assignment]

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "leave_space",
            "signer_slug": "alice-0001",
            "event_id": "ev_dead_server",
            "payload": {"space_id": "sp_1"},
        },
    )

    assert client._queue.qsize() == 0
    await store.close()


# ─── manual-accept inviter cache backfill ────────


@pytest.mark.asyncio
async def test_invite_to_channel_records_inviter_in_cache():
    """Inbound ``invite_to_channel`` for a non-self invitee should
    seed the cache so a later manual-accept (no ``original_invite``)
    can still render '(invited by X)'."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "invite_to_channel",
            "signer_slug": "alice-0001",  # inviter
            "event_id": "ev_invite_42",
            "payload": {
                "space_id": "sp_1",
                "channel_id": "ch_1",
                "invitee_slug": "bob-0002",  # not us
            },
        },
    )

    assert client._inviter_by_invitation_event_id == {
        "ev_invite_42": "alice-0001",
    }
    assert client._queue.qsize() == 0
    await store.close()


@pytest.mark.asyncio
async def test_manual_accept_channel_invite_falls_back_to_cache_for_inviter():
    """No original_invite → resolve inviter via the cache."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"
    client._inviter_by_invitation_event_id["ev_invite_42"] = "alice-0001"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_channel_invite",
            "signer_slug": "bob-0002",  # the joiner, manually accepting
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "invitation_event_id": "ev_invite_42",
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "joined channel #general" in msg["text"]
    assert "(invited by " in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_manual_accept_channel_invite_renders_no_inviter_on_cache_miss():
    """Cache miss (e.g. restart between invite + accept) → no suffix."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_channel_invite",
            "signer_slug": "bob-0002",
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "invitation_event_id": "ev_missing",
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "joined channel #general" in msg["text"]
    assert "invited by" not in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_original_invite_takes_precedence_over_cache():
    """original_invite wins over the cache when both are present."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"
    client._inviter_by_invitation_event_id["ev_invite_42"] = "stale-cache-0000"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_channel_invite",
            "signer_slug": "bob-0002",
            "payload": {
                "channel_id": "ch_1",
                "space_id": "sp_1",
                "invitation_event_id": "ev_invite_42",
                "original_invite": {"signer_slug": "alice-0001"},
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Alice" in msg["text"]
    assert "stale-cache-0000" not in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_invite_to_space_records_inviter_for_later_space_accept():
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "invite_to_space",
            "signer_slug": "alice-0001",
            "event_id": "ev_space_invite_7",
            "payload": {
                "space_id": "sp_1",
                "invitee_slug": "bob-0002",
            },
        },
    )

    assert (
        client._inviter_by_invitation_event_id["ev_space_invite_7"]
        == "alice-0001"
    )

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "accept_space_invite",
            "signer_slug": "bob-0002",
            "payload": {
                "space_id": "sp_1",
                "invitation_event_id": "ev_space_invite_7",
            },
        },
    )

    _, _, root_id = await client._queue.get()
    msg = client._thread_state[root_id].messages[0]
    assert "Bob" in msg["text"]
    assert "joined space" in msg["text"]
    assert "(invited by " in msg["text"]
    assert "Alice" in msg["text"]
    await store.close()


@pytest.mark.asyncio
async def test_invite_record_skips_when_event_id_or_signer_missing():
    """Malformed invite must not poison the cache with empty keys/values."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "invite_to_channel",
            "signer_slug": "",  # missing inviter
            "event_id": "ev_no_signer",
            "payload": {
                "space_id": "sp_1",
                "channel_id": "ch_1",
                "invitee_slug": "bob-0002",
            },
        },
    )
    await client._handle_event(
        scope="sp_1",
        event={
            "kind": "invite_to_channel",
            "signer_slug": "alice-0001",
            # event_id missing
            "payload": {
                "space_id": "sp_1",
                "channel_id": "ch_1",
                "invitee_slug": "bob-0002",
            },
        },
    )

    assert client._inviter_by_invitation_event_id == {}
    await store.close()


@pytest.mark.asyncio
async def test_space_membership_dedups_on_duplicate_event_id():
    """Reconnect-replay → announce once."""
    store = await _make_store()
    client = _make_client(store)
    client._channel_space["ch_1"] = "sp_1"

    event = {
        "kind": "leave_space",
        "signer_slug": "alice-0001",
        "event_id": "ev_space_replay",
        "payload": {"space_id": "sp_1"},
    }

    await client._handle_event(scope="sp_1", event=event)
    await client._handle_event(scope="sp_1", event=event)

    assert client._queue.qsize() == 1
    assert "ev_space_replay" in client._processed_membership_event_ids
    await store.close()
