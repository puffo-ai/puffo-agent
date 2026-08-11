"""Durable channel-membership system-message behavior."""

from __future__ import annotations

import logging

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.message_projection import format_message_group
from puffo_agent.agent.global_inbox_runtime import route_for
from puffo_agent.agent.inbox_scheduler import InboxPlanner
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


class RuntimeSpy:
    def __init__(self):
        self.work = 0
        self.delivery = 0

    def notify(self):
        self.work += 1

    def notify_delivery(self):
        self.delivery += 1


async def _client(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.store = store
    client.global_runtime = RuntimeSpy()
    client._log = logging.getLogger(__name__)
    client._channel_space = {"channel": "space"}
    client._space_name_cache = {}
    client._channel_name_cache = {}
    client._processed_membership_event_ids = set()
    client._inviter_by_invitation_event_id = {}

    async def space_name(_space_id):
        return "Team"

    async def channel_name(*, space_id, channel_id):
        return "general"

    async def display_name(slug):
        return {
            "alice": "Alice",
            "operator": "Operator",
        }.get(slug, "")

    client._resolve_space_name = space_name
    client._resolve_channel_name = channel_name
    client._fetch_display_name = display_name
    return client, store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "event_type", "fragment"),
    [
        ("joined", "channel_member_joined", "**Alice**(@alice) joined channel #general"),
        ("left", "channel_member_left", "**Alice**(@alice) left channel #general"),
        (
            "removed",
            "channel_member_removed",
            "**Alice**(@alice) was removed from channel #general "
            "by **Operator**(@operator)",
        ),
    ],
)
async def test_membership_rendering_persists_and_wakes_work_once(
    tmp_path, action, event_type, fragment
):
    client, store = await _client(tmp_path)
    await client._enqueue_membership_system_message(
        channel_id="channel",
        actor_slug="alice",
        action=action,
        kicker_slug="operator",
        event_id=f"event-{action}",
    )
    rows = await store.get_pending()
    assert len(rows) == 1
    assert rows[0].sender_slug == "system"
    assert rows[0].channel_id == "channel"
    assert rows[0].content["event_type"] == event_type
    assert rows[0].content["actor_slug"] == "alice"
    assert rows[0].content["subject_ref"] == "channel:space:channel"
    assert fragment in rows[0].content["text"]
    projected = format_message_group(rows)
    assert 'target_type="channel"' in projected
    assert f'event_type="{event_type}"' in projected
    assert 'actor_identity="@alice"' in projected
    assert route_for(rows[0]).kind == "channel"
    assert store.target_projection(rows[0]) == "channel:space:channel"
    assert InboxPlanner.target_projection(rows[0]) == (
        "channel", "space", "channel"
    )
    assert client.global_runtime.work == 1
    assert client.global_runtime.delivery == 0
    await store.close()


@pytest.mark.asyncio
async def test_joined_membership_cites_inviter(tmp_path):
    client, store = await _client(tmp_path)
    await client._enqueue_membership_system_message(
        channel_id="channel",
        actor_slug="alice",
        action="joined",
        inviter_slug="operator",
        event_id="event-inviter",
    )
    row = (await store.get_pending())[0]
    assert "invited by **Operator**(@operator)" in row.content["text"]
    assert row.content["inviter_slug"] == "operator"
    await store.close()


@pytest.mark.asyncio
async def test_unknown_action_and_persistence_failure_do_not_wake(tmp_path):
    client, store = await _client(tmp_path)
    await client._enqueue_membership_system_message(
        channel_id="channel",
        actor_slug="alice",
        action="unknown",
    )
    assert await store.get_pending() == ()
    assert client.global_runtime.work == 0

    async def fail(*_args, **_kwargs):
        raise OSError("disk full")

    store.store_local_event = fail
    await client._enqueue_membership_system_message(
        channel_id="channel",
        actor_slug="alice",
        action="joined",
    )
    assert client.global_runtime.work == 0
    assert client.global_runtime.delivery == 0
    await store.close()
