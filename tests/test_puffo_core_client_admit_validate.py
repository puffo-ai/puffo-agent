"""PUF-227-A: receiver-side strict cache-validation on admit.

When an incoming envelope arrives, ``handle_envelope`` calls
``_validate_incoming_parent_id`` on both ``thread_root_id`` and
``reply_to_id``. Anything that doesn't point to a same-channel
parent in our local ``message_store`` gets wiped to ``None``
before being stored / queued. Same Scout-class threat-model as the
sender side, enforced symmetrically.

These tests exercise the helper directly via a minimally-
constructed ``PuffoCoreMessageClient`` (the full WS / decryption
stack isn't needed to exercise the validation logic).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _make_store() -> MessageStore:
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    return store


def _bare_client(store: MessageStore) -> PuffoCoreMessageClient:
    """Build a PuffoCoreMessageClient with just enough state to
    exercise ``_validate_incoming_parent_id``. Bypasses __init__
    because the real constructor needs a keystore + http + WS
    bookkeeping we don't need here."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.store = store
    return client


async def _seed_parent(
    store: MessageStore,
    *,
    envelope_id: str,
    channel_id: str | None,
    space_id: str | None,
) -> None:
    await store.store({
        "envelope_id": envelope_id,
        "envelope_kind": "channel" if channel_id else "dm",
        "sender_slug": "sam-0001",
        "channel_id": channel_id,
        "space_id": space_id,
        "content_type": "text/plain",
        "content": "parent root post",
        "sent_at": _now_ms(),
        "thread_root_id": None,
        "reply_to_id": None,
    })


# ── pass-through ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_passes_through_when_parent_id_is_none():
    store = await _make_store()
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(None, "ch_x", "sp_1")
    assert out is None
    out = await client._validate_incoming_parent_id("", "ch_x", "sp_1")
    assert out == ""
    await store.close()


@pytest.mark.asyncio
async def test_validate_preserves_id_when_parent_in_same_channel():
    """Valid same-channel parent → id passes through; agent sees
    the thread linkage."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_root", "ch_gtm", "sp_1",
    )
    assert out == "env_root"
    await store.close()


# ── wipes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_not_in_local_cache():
    """Strict-(a): parent envelope unknown to this client → wipe.
    Forward-traffic enforcement, no migration of historical state."""
    store = await _make_store()
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_unknown", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_in_different_channel():
    """Scout's PUF-227 symptom on the receiver side. Local cache
    HAS the parent envelope but it lives in a different channel
    than the incoming envelope claims — strict invariant says wipe.
    Without this wipe, the agent's batch coalescer would inherit
    the parent's channel context for the new envelope (the exact
    Scout-class symptom)."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root_general",
        channel_id="ch_general", space_id="sp_1",
    )
    client = _bare_client(store)
    # Incoming claims thread_root_id=env_root_general but arrives
    # in channel ch_gtm.
    out = await client._validate_incoming_parent_id(
        "env_root_general", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_when_parent_in_different_space():
    """Cross-space parent → also wiped (belt-and-braces alongside
    channel-mismatch)."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other_space",
        channel_id="ch_gtm", space_id="sp_OTHER",
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_other_space", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


# ── DM context ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_dm_envelope_skips_channel_check_but_keeps_cache_check():
    """Incoming DM envelope (no channel_id). Cache-presence check
    still fires; channel-match check is naturally a no-op when
    expected_channel_id is None."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_dm_root", channel_id=None, space_id=None,
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_dm_root", None, None,
    )
    assert out == "env_dm_root"
    # Unknown DM parent → wipe.
    out = await client._validate_incoming_parent_id(
        "env_dm_unknown", None, None,
    )
    assert out is None
    await store.close()


# ── lookup transport error ────────────────────────────────────────


# ── dual-call shape (reply_to_id symmetry) ────────────────────────


@pytest.mark.asyncio
async def test_validate_reply_to_id_uses_same_helper():
    """PUF-227-A handle_envelope calls _validate_incoming_parent_id
    on BOTH thread_root_id AND reply_to_id with the same channel /
    space expectations. This test pins that dual-call shape: the
    same helper, the same args, applied to both fields, with the
    same wipe semantics. Operator's review #1 ask — without this
    test a future refactor could accidentally drop the reply_to_id
    side and the regression wouldn't surface until a customer hit
    a cross-channel reply chain."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other_chan",
        channel_id="ch_other", space_id="sp_1",
    )
    client = _bare_client(store)

    # Same parent envelope, same outbound channel/space args, same
    # cross-channel mismatch — applied to both id roles. Both return
    # None (wiped) under the strict invariant.
    thread_wiped = await client._validate_incoming_parent_id(
        "env_other_chan", "ch_gtm", "sp_1",
    )
    reply_wiped = await client._validate_incoming_parent_id(
        "env_other_chan", "ch_gtm", "sp_1",
    )
    assert thread_wiped is None
    assert reply_wiped is None

    # And the same helper preserves both when same-channel.
    await _seed_parent(
        store, envelope_id="env_same_chan",
        channel_id="ch_gtm", space_id="sp_1",
    )
    thread_kept = await client._validate_incoming_parent_id(
        "env_same_chan", "ch_gtm", "sp_1",
    )
    reply_kept = await client._validate_incoming_parent_id(
        "env_same_chan", "ch_gtm", "sp_1",
    )
    assert thread_kept == "env_same_chan"
    assert reply_kept == "env_same_chan"
    await store.close()


@pytest.mark.asyncio
async def test_validate_wipes_on_store_lookup_exception(monkeypatch):
    """Sqlite hiccup mid-lookup → strict mode wipes rather than
    shipping an unverifiable id."""
    store = await _make_store()
    client = _bare_client(store)

    async def boom(envelope_id):
        raise RuntimeError("simulated sqlite hiccup")

    monkeypatch.setattr(store, "get_message_by_envelope", boom)
    out = await client._validate_incoming_parent_id(
        "env_anything", "ch_x", "sp_1",
    )
    assert out is None
    await store.close()


# ── thread-root normalization (admit-time) ────────────────────────
#
# thread_root_id additionally resolves to the canonical root so a sender
# stamping a reply id can't index the row under a non-root. reply_to_id
# keeps naming the direct parent.


async def _seed_reply(
    store: MessageStore,
    *,
    envelope_id: str,
    thread_root_id: str | None,
    channel_id: str | None,
    space_id: str | None,
) -> None:
    await store.store({
        "envelope_id": envelope_id,
        "envelope_kind": "channel" if channel_id else "dm",
        "sender_slug": "sam-0001",
        "channel_id": channel_id,
        "space_id": space_id,
        "content_type": "text/plain",
        "content": f"reply {envelope_id}",
        "sent_at": _now_ms(),
        "thread_root_id": thread_root_id,
        "reply_to_id": None,
    })


@pytest.mark.asyncio
async def test_resolve_corrects_a_reply_pointer_to_the_real_root():
    """Rule 1: same channel but not a root → walk up to the root."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_reply", thread_root_id="env_root",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_reply", "ch_gtm", "sp_1",
    )
    assert out == "env_root"
    await store.close()


@pytest.mark.asyncio
async def test_resolve_walks_a_multi_hop_corrupt_chain():
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_a", thread_root_id="env_root",
        channel_id="ch_gtm", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_b", thread_root_id="env_a",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_b", "ch_gtm", "sp_1",
    )
    assert out == "env_root"
    await store.close()


@pytest.mark.asyncio
async def test_resolve_passes_through_a_real_root():
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_root", "ch_gtm", "sp_1",
    )
    assert out == "env_root"
    await store.close()


@pytest.mark.asyncio
async def test_resolve_treats_a_self_rooted_envelope_as_a_root():
    """The synthetic system envelopes (intro nudge) store their own id as
    thread_root_id — that's a root, not a cycle."""
    store = await _make_store()
    await _seed_reply(
        store, envelope_id="env_intro", thread_root_id="env_intro",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_intro", "ch_gtm", "sp_1",
    )
    assert out == "env_intro"
    await store.close()


@pytest.mark.asyncio
async def test_resolve_wipes_a_cross_channel_root():
    """Rule 2: different channel → no root, admit as a new root."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other", channel_id="ch_other", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_other", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_resolve_wipes_when_a_mid_chain_hop_leaves_the_channel():
    """The walk re-checks scope at every hop, not just the first."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_other", channel_id="ch_other", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_reply", thread_root_id="env_other",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_reply", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_resolve_wipes_when_root_not_in_local_cache():
    store = await _make_store()
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_missing", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_resolve_wipes_on_a_cycle():
    store = await _make_store()
    await _seed_reply(
        store, envelope_id="env_a", thread_root_id="env_b",
        channel_id="ch_gtm", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_b", thread_root_id="env_a",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_a", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_resolve_wipes_a_chain_deeper_than_the_cap():
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_0", channel_id="ch_gtm", space_id="sp_1",
    )
    for i in range(1, 10):
        await _seed_reply(
            store, envelope_id=f"env_{i}", thread_root_id=f"env_{i - 1}",
            channel_id="ch_gtm", space_id="sp_1",
        )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_9", "ch_gtm", "sp_1",
    )
    assert out is None
    await store.close()


@pytest.mark.asyncio
async def test_resolve_works_for_dms_where_channel_id_is_none():
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_dm_root", channel_id=None, space_id=None,
    )
    await _seed_reply(
        store, envelope_id="env_dm_reply", thread_root_id="env_dm_root",
        channel_id=None, space_id=None,
    )
    client = _bare_client(store)
    out = await client._resolve_incoming_thread_root(
        "env_dm_reply", None, None,
    )
    assert out == "env_dm_root"
    await store.close()


@pytest.mark.asyncio
async def test_reply_to_id_still_names_the_direct_parent():
    """Only thread_root_id normalizes — reply_to_id must NOT walk up."""
    store = await _make_store()
    await _seed_parent(
        store, envelope_id="env_root", channel_id="ch_gtm", space_id="sp_1",
    )
    await _seed_reply(
        store, envelope_id="env_reply", thread_root_id="env_root",
        channel_id="ch_gtm", space_id="sp_1",
    )
    client = _bare_client(store)
    out = await client._validate_incoming_parent_id(
        "env_reply", "ch_gtm", "sp_1",
    )
    assert out == "env_reply"
    await store.close()
