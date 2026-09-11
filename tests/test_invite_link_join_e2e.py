"""End-to-end: server-synthesized ``add_to_channel`` WS frames.

puffo-server #325 emits one synthetic ``add_to_channel`` event per
channel joined through an invite-link redemption and pushes it only
to that channel's active members. These tests drive the real frame
path — raw WS JSON through ``PuffoCoreWsClient._handle_frame`` into
``PuffoCoreMessageClient._handle_event`` — and assert on the durable
system message in a real sqlite ``MessageStore`` plus its runtime
projection/routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from puffo_agent.agent.global_inbox_runtime import route_for
from puffo_agent.agent.message_projection import format_message_group
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.ws_client import PuffoCoreWsClient


class RuntimeSpy:
    def __init__(self):
        self.work = 0
        self.delivery = 0

    def notify(self):
        self.work += 1

    def notify_delivery(self):
        self.delivery += 1


async def _wired_stack(tmp_path, *, channel_space=None, warm_reveals=None):
    """Real MessageStore + core client, WS client wired to it.

    ``warm_reveals`` is what the (stubbed) member-cache warm learns
    from the server; the real ``rewarm_channel_caches`` (lock +
    debounce) runs on cache misses.
    """
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.store = store
    client.global_runtime = RuntimeSpy()
    client._log = logging.getLogger(__name__)
    client._channel_space = (
        {"ch_general": "sp_1", "ch_priv": "sp_1"}
        if channel_space is None
        else channel_space
    )
    client._rewarm_lock = asyncio.Lock()
    client._last_rewarm = 0.0
    client._warm_calls = 0

    async def warm():
        client._warm_calls += 1
        client._channel_space.update(warm_reveals or {})

    client._warm_member_caches = warm
    client._space_name_cache = {}
    client._channel_name_cache = {}
    client._channel_encrypted = {}
    client._space_members = {}
    client._processed_membership_event_ids = set()
    client._inviter_by_invitation_event_id = {}

    async def space_name(_space_id):
        return "Team"

    async def channel_name(*, space_id, channel_id):
        return {"ch_general": "general", "ch_priv": "secrets"}.get(
            channel_id, channel_id
        )

    async def display_name(slug):
        return {"bob-0002": "Bob", "owner-0001": "Owner"}.get(slug, "")

    client._resolve_space_name = space_name
    client._resolve_channel_name = channel_name
    client._fetch_display_name = display_name

    ws = PuffoCoreWsClient.__new__(PuffoCoreWsClient)
    ws.on_event = client._handle_event
    return ws, client, store


def _join_frame(
    channel_id: str = "ch_general",
    event_id: str = "ev_add_1",
    added_slug: str = "bob-0002",
) -> str:
    return json.dumps({
        "type": "event",
        "scope": "sp_1",
        "event": {
            "event_type": "signed_event",
            "version": 1,
            "event_id": event_id,
            "kind": "add_to_channel",
            "signer_slug": "owner-0001",
            "signer_device_id": "",
            "signer_subkey_id": "",
            "signature": "server-auto:invite-capability-add-to-channel",
            "payload": {
                "space_id": "sp_1",
                "channel_id": channel_id,
                "added_slug": added_slug,
                "issued_at": 1_700_000_000_000,
                "nonce": "n0nce",
                "source_invite_id": "inv_1",
                "source_create_event_id": "ev_create_1",
                "source_redemption_id": "red_1",
                "source_redemption_event_id": "ev_redeem_1",
            },
        },
    })


@pytest.mark.asyncio
async def test_join_frame_persists_membership_message_and_wakes_runtime(tmp_path):
    ws, client, store = await _wired_stack(tmp_path)

    await ws._handle_frame(_join_frame())

    rows = await store.get_pending()
    assert len(rows) == 1
    row = rows[0]
    assert row.sender_slug == "system"
    assert row.channel_id == "ch_general"
    assert row.content["event_type"] == "channel_member_joined"
    assert row.content["actor_slug"] == "bob-0002"
    assert row.content["inviter_slug"] == "owner-0001"
    assert row.content["subject_ref"] == "channel:sp_1:ch_general"
    assert (
        "**Bob**(@bob-0002) joined channel #general "
        "(invited by **Owner**(@owner-0001))." in row.content["text"]
    )
    projected = format_message_group(rows)
    assert 'event_type="channel_member_joined"' in projected
    assert route_for(row).kind == "channel"
    assert client.global_runtime.work == 1
    assert client.global_runtime.delivery == 0
    await store.close()


@pytest.mark.asyncio
async def test_private_channel_join_frame_persists_for_member_agent(tmp_path):
    ws, client, store = await _wired_stack(tmp_path)

    await ws._handle_frame(_join_frame(channel_id="ch_priv", event_id="ev_add_2"))

    rows = await store.get_pending()
    assert len(rows) == 1
    assert rows[0].channel_id == "ch_priv"
    assert "joined channel #secrets" in rows[0].content["text"]
    await store.close()


@pytest.mark.asyncio
async def test_redelivered_join_frame_persists_once(tmp_path):
    ws, client, store = await _wired_stack(tmp_path)
    frame = _join_frame()

    await ws._handle_frame(frame)
    await ws._handle_frame(frame)

    assert len(await store.get_pending()) == 1
    assert client.global_runtime.work == 1
    await store.close()


@pytest.mark.asyncio
async def test_join_frame_for_unknown_channel_persists_nothing(tmp_path):
    """Agent isn't a member of the channel: a rewarm runs (the miss
    could be the connect-time warm race) but still doesn't list the
    channel, so nothing durable and no wake."""
    ws, client, store = await _wired_stack(tmp_path)

    await ws._handle_frame(_join_frame(channel_id="ch_not_mine"))

    assert client._warm_calls == 1
    assert await store.get_pending() == ()
    assert client.global_runtime.work == 0
    await store.close()


@pytest.mark.asyncio
async def test_join_frame_racing_connect_warm_rewarms_and_persists(tmp_path):
    """Regression: on first connect ``_channel_space`` starts empty
    and the warm task is fire-and-forget. A join frame arriving first
    must trigger a rewarm and still produce the system message."""
    ws, client, store = await _wired_stack(
        tmp_path,
        channel_space={},
        warm_reveals={"ch_general": "sp_1"},
    )

    await ws._handle_frame(_join_frame())

    assert client._warm_calls == 1
    rows = await store.get_pending()
    assert len(rows) == 1
    assert rows[0].channel_id == "ch_general"
    assert "joined channel #general" in rows[0].content["text"]
    assert client.global_runtime.work == 1
    await store.close()


@pytest.mark.asyncio
async def test_join_frame_missing_added_slug_persists_nothing(tmp_path):
    ws, client, store = await _wired_stack(tmp_path)
    frame = json.loads(_join_frame())
    del frame["event"]["payload"]["added_slug"]

    await ws._handle_frame(json.dumps(frame))

    assert await store.get_pending() == ()
    assert client.global_runtime.work == 0
    await store.close()


@pytest.mark.asyncio
async def test_store_failure_logs_without_waking_runtime(tmp_path):
    """A persistence failure is logged and never wakes the runtime.

    Pins current (pre-existing) behavior shared by every membership
    kind: ``_persist_membership_message`` swallows the store error,
    so the event id still lands in the processed set and a WS
    redelivery won't rewrite the lost message.
    """
    ws, client, store = await _wired_stack(tmp_path)

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    store.store_local_event = fail
    frame = _join_frame()

    await ws._handle_frame(frame)

    assert client.global_runtime.work == 0
    assert "ev_add_1" in client._processed_membership_event_ids
    store.store_local_event = MessageStore.store_local_event.__get__(store)
    assert await store.get_pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_multi_channel_grant_persists_each_once(tmp_path):
    """One redemption granting two channels: both frames may race in;
    each channel gets exactly one durable message."""
    ws, client, store = await _wired_stack(tmp_path)

    await asyncio.gather(
        ws._handle_frame(_join_frame(channel_id="ch_general", event_id="ev_a")),
        ws._handle_frame(_join_frame(channel_id="ch_priv", event_id="ev_b")),
        ws._handle_frame(_join_frame(channel_id="ch_general", event_id="ev_a")),
    )

    rows = await store.get_pending()
    assert sorted(row.channel_id for row in rows) == ["ch_general", "ch_priv"]
    assert client.global_runtime.work == 2
    await store.close()


@pytest.mark.asyncio
async def test_join_frame_after_recent_rewarm_bypasses_debounce(tmp_path):
    """Regression: a warm can finish moments before the grant lands
    server-side. The event-driven recheck must bypass the 5s rewarm
    debounce, or the one-shot WS event is dropped for good."""
    ws, client, store = await _wired_stack(
        tmp_path,
        channel_space={},
        warm_reveals={"ch_general": "sp_1"},
    )
    client._last_rewarm = time.monotonic()  # a rewarm "just ran"

    await ws._handle_frame(_join_frame())

    assert client._warm_calls == 1
    rows = await store.get_pending()
    assert len(rows) == 1
    assert rows[0].channel_id == "ch_general"
    assert client.global_runtime.work == 1
    await store.close()


@pytest.mark.asyncio
async def test_unforced_rewarm_keeps_debounce(tmp_path):
    """The shared on-miss rewarm entry point stays debounced for its
    other (non-event) callers; only ``force`` bypasses it."""
    ws, client, store = await _wired_stack(tmp_path)

    await client.rewarm_channel_caches()
    await client.rewarm_channel_caches()
    assert client._warm_calls == 1

    await client.rewarm_channel_caches(force=True)
    assert client._warm_calls == 2
    await store.close()
