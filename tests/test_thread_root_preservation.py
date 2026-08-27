"""A locally unverifiable thread root is preserved, not erased.

The live inbound path used to wipe a reply's thread_root_id whenever the
claimed root was not in the local store (root predates the agent joining,
aged out, failed to decrypt) — permanently demoting the reply to a
channel-level message. The claim is now kept with an ``unverified`` mark,
affirmative ownership mismatches still wipe, and the root's later arrival
runs the deferred check.
"""

import logging
import os
import tempfile

import pytest

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.message_store_models import ReceiptDisposition
from puffo_agent.agent.message_projection import format_message_group
from puffo_agent.agent.thread_context import resolve_incoming_thread_root

log = logging.getLogger("test")


def _temp_store() -> MessageStore:
    d = tempfile.mkdtemp()
    return MessageStore(os.path.join(d, "messages.db"))


def _payload(envelope_id, *, channel_id="ch_1", space_id="sp_1", root=None,
             root_unverified=False, kind="channel", sender="alice-0001",
             recipient=""):
    return {
        "envelope_id": envelope_id,
        "envelope_kind": kind,
        "sender_slug": sender,
        "channel_id": channel_id if kind == "channel" else "",
        "space_id": space_id if kind == "channel" else "",
        "recipient_slug": recipient,
        "content_type": "text/plain",
        "content": f"Message {envelope_id}",
        "sent_at": 1000,
        "thread_root_id": root,
        "thread_root_unverified": root_unverified,
    }


async def _receive(store, envelope_id, seq, **kwargs):
    await store.store_receipt(
        _payload(envelope_id, **kwargs),
        server_seq=seq,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="ok",
    )


@pytest.mark.asyncio
async def test_missing_root_is_kept_unverified():
    store = _temp_store()
    root, unverified = await resolve_incoming_thread_root(
        store=store, log=log, parent_id="ghost-root",
        expected_channel_id="ch_1", expected_space_id="sp_1",
        expected_envelope_kind="channel",
    )
    assert (root, unverified) == ("ghost-root", True)
    await store.close()


@pytest.mark.asyncio
async def test_ownership_mismatch_still_wipes():
    store = _temp_store()
    await _receive(store, "other-root", 1, channel_id="ch_OTHER")
    root, unverified = await resolve_incoming_thread_root(
        store=store, log=log, parent_id="other-root",
        expected_channel_id="ch_1", expected_space_id="sp_1",
        expected_envelope_kind="channel",
    )
    assert (root, unverified) == (None, False)
    await store.close()


@pytest.mark.asyncio
async def test_unverified_claim_round_trips_and_projects():
    store = _temp_store()
    await _receive(store, "reply-1", 1, root="ghost-root", root_unverified=True)
    row = await store.get_message_by_envelope("reply-1")
    assert row.thread_root_id == "ghost-root"
    assert row.thread_root_unverified
    assert "thread_root_unverified=true" in format_message_group([row])
    await store.close()


@pytest.mark.asyncio
async def test_root_arrival_settles_matching_claims():
    store = _temp_store()
    await _receive(store, "reply-1", 1, root="late-root", root_unverified=True)
    await _receive(store, "late-root", 2)
    row = await store.get_message_by_envelope("reply-1")
    assert row.thread_root_id == "late-root"
    assert not row.thread_root_unverified
    await store.close()


@pytest.mark.asyncio
async def test_root_arrival_in_other_channel_wipes_claim():
    store = _temp_store()
    await _receive(store, "reply-1", 1, root="foreign-root", root_unverified=True)
    await _receive(store, "foreign-root", 2, channel_id="ch_OTHER")
    row = await store.get_message_by_envelope("reply-1")
    assert row.thread_root_id is None
    assert not row.thread_root_unverified
    await store.close()


@pytest.mark.asyncio
async def test_claimed_root_that_is_a_reply_reroots_claimants():
    store = _temp_store()
    await _receive(store, "true-root", 1)
    await _receive(store, "reply-1", 2, root="mid-reply", root_unverified=True)
    await _receive(store, "mid-reply", 3, root="true-root")
    row = await store.get_message_by_envelope("reply-1")
    assert row.thread_root_id == "true-root"
    assert not row.thread_root_unverified
    await store.close()


@pytest.mark.asyncio
async def test_dm_root_arrival_settles_same_conversation_claim():
    store = _temp_store()
    await _receive(store, "dm-reply", 1, kind="dm", sender="alice-0001",
                   recipient="self-0001", root="dm-root", root_unverified=True)
    await _receive(store, "dm-root", 2, kind="dm", sender="self-0001",
                   recipient="alice-0001")
    row = await store.get_message_by_envelope("dm-reply")
    assert row.thread_root_id == "dm-root"
    assert not row.thread_root_unverified
    await store.close()


@pytest.mark.asyncio
async def test_dm_root_arrival_from_other_conversation_wipes_claim():
    store = _temp_store()
    await _receive(store, "dm-reply", 1, kind="dm", sender="alice-0001",
                   recipient="self-0001", root="dm-root", root_unverified=True)
    await _receive(store, "dm-root", 2, kind="dm", sender="mallory-0002",
                   recipient="self-0001")
    row = await store.get_message_by_envelope("dm-reply")
    assert row.thread_root_id is None
    assert not row.thread_root_unverified
    await store.close()


@pytest.mark.asyncio
async def test_thread_read_works_without_the_root_row():
    store = _temp_store()
    await _receive(store, "reply-1", 1, root="ghost-root", root_unverified=True)
    rows = await store.get_thread_messages("ghost-root")
    assert [r.envelope_id for r in rows] == ["reply-1"]
    await store.close()


@pytest.mark.asyncio
async def test_inbound_preserved_root_is_replyable_outbound():
    """End-to-end flow closure: receive a reply whose root is locally
    unknown (kept unverified), then resolve an outbound reply into the
    same thread — it must keep the claimed root, not degrade to a
    channel-level send."""
    from puffo_agent.mcp.puffo_core_tools import _resolve_outgoing_root

    store = _temp_store()
    await _receive(store, "reply-1", 1, root="missing-root",
                   root_unverified=True)
    resolved, note = await _resolve_outgoing_root(
        "missing-root", store, self_slug="agent-0001",
        channel_id="ch_1", space_id="sp_1", dm_peer=None,
    )
    assert (resolved, note) == ("missing-root", "")
    await store.close()


@pytest.mark.asyncio
async def test_verified_rows_are_untouched_by_arrivals():
    store = _temp_store()
    await _receive(store, "root-a", 1)
    await _receive(store, "reply-a", 2, root="root-a")
    await _receive(store, "root-a-dup", 3)
    row = await store.get_message_by_envelope("reply-a")
    assert row.thread_root_id == "root-a"
    assert not row.thread_root_unverified
    await store.close()
