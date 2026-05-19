"""WS routing for space/channel membership-exit events.

Pairs with ``puffo-server`` review/events PR #74: the server now
broadcasts ``leave_space`` / ``remove_from_space`` /
``leave_channel`` / ``remove_from_channel`` / ``cancel_space_invite``
/ ``cancel_channel_invite`` to the agent's own session even after
the agent is no longer in the space's member set (``extra_ws_targets``
union for the removed slug + each cascaded agent). These tests pin
the agent-side reactions: cache eviction, operator-DM notification,
and "not for me" no-ops.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


# ─── Fixture ───────────────────────────────────────────────────────


def _make_client(operator_slug: str = "op-1") -> tuple[PuffoCoreMessageClient, list[dict]]:
    """Bare client with just enough state to exercise the WS router.

    Stubs:
      * ``_send_dm`` records calls in ``sent`` instead of round-tripping.
      * ``_resolve_space_name`` / ``_resolve_channel_name`` /
        ``_fetch_display_name`` return canonical labels so DM text
        assertions don't depend on /spaces or /identities lookups.

    Returns the client + a list the stubbed ``_send_dm`` appends to.
    """
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client._channel_space = {}
    client._space_name_cache = {}
    client._channel_name_cache = {}
    client._processed_invite_ids = set()
    client._pending_invite_dms = {}

    sent: list[dict] = []

    async def _stub_send_dm(recipient_slug: str, text: str, root_id: str) -> dict | None:
        sent.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_response_{len(sent)}"}

    async def _stub_space_name(space_id: str) -> str:
        return {"sp_1": "Team", "sp_2": "Other"}.get(space_id, space_id)

    async def _stub_channel_name(*, space_id: str, channel_id: str) -> str:
        return {"ch_1": "general", "ch_priv": "secrets"}.get(channel_id, channel_id)

    async def _stub_display_name(slug: str) -> str:
        return {"alice-0001": "Alice", "op-1": "Operator"}.get(slug, "")

    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]
    return client, sent


# ─── leave_space (synthetic cascade + self-signed) ─────────────────


@pytest.mark.asyncio
async def test_leave_space_synthetic_cascade_evicts_caches_and_dms_operator():
    """puffo-server #74 emits a synthetic LeaveSpace per cascaded
    agent when its operator leaves. ``signature`` is the audit marker.
    Agent must evict per-space caches AND DM operator explaining the
    cascade."""
    client, sent = _make_client()
    client._channel_space["ch_1"] = "sp_1"
    client._channel_space["ch_other"] = "sp_2"  # unrelated; survives
    client._channel_name_cache["ch_1"] = "general"
    client._space_name_cache["sp_1"] = "Team"

    event = {
        "kind": "leave_space",
        "signer_slug": "agent-1",
        "signature": "server-auto:agent-cascade-leave-space",
        "payload": {"space_id": "sp_1"},
    }
    await client._handle_event(scope="sp_1", event=event)

    # Per-space caches gone, unrelated space untouched.
    assert "ch_1" not in client._channel_space
    assert "ch_1" not in client._channel_name_cache
    assert "sp_1" not in client._space_name_cache
    assert client._channel_space.get("ch_other") == "sp_2"

    # One operator DM, mentions the cascade reason.
    assert len(sent) == 1
    assert sent[0]["to"] == "op-1"
    assert "Team" in sent[0]["text"]
    assert "sp_1" in sent[0]["text"]
    assert "cascaded" in sent[0]["text"]


@pytest.mark.asyncio
async def test_leave_space_self_signed_dm_mentions_self_action():
    """Non-synthetic LeaveSpace signed by the agent itself — different
    wording so the operator doesn't see "cascaded" when there was no
    cascade."""
    client, sent = _make_client()
    event = {
        "kind": "leave_space",
        "signer_slug": "agent-1",
        "signature": "real-ed25519-sig",
        "payload": {"space_id": "sp_1"},
    }
    await client._handle_event(scope="sp_1", event=event)
    assert len(sent) == 1
    assert "signed a LeaveSpace" in sent[0]["text"]


@pytest.mark.asyncio
async def test_leave_space_for_other_slug_is_noop():
    """Someone else's LeaveSpace gets fanned out to the agent too
    (the agent is still a space member at fan-out time). Must NOT
    fire the DM."""
    client, sent = _make_client()
    event = {
        "kind": "leave_space",
        "signer_slug": "bob-0001",
        "signature": "real-ed25519-sig",
        "payload": {"space_id": "sp_1"},
    }
    await client._handle_event(scope="sp_1", event=event)
    assert sent == []


# ─── remove_from_space ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_from_space_evicts_caches_and_dms_with_kicker():
    client, sent = _make_client()
    client._channel_space["ch_1"] = "sp_1"
    client._channel_name_cache["ch_1"] = "general"
    client._space_name_cache["sp_1"] = "Team"

    event = {
        "kind": "remove_from_space",
        "signer_slug": "alice-0001",
        "payload": {"space_id": "sp_1", "removed_slug": "agent-1"},
    }
    await client._handle_event(scope="sp_1", event=event)

    assert "ch_1" not in client._channel_space
    assert "sp_1" not in client._space_name_cache
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "Team" in text
    assert "Alice" in text  # _fetch_display_name resolved
    assert "alice-0001" in text


@pytest.mark.asyncio
async def test_remove_from_space_other_target_is_noop():
    """Owner kicked some OTHER member; we got the WS push as a
    surviving member of the space. No-op."""
    client, sent = _make_client()
    client._channel_space["ch_1"] = "sp_1"
    event = {
        "kind": "remove_from_space",
        "signer_slug": "alice-0001",
        "payload": {"space_id": "sp_1", "removed_slug": "bob-0001"},
    }
    await client._handle_event(scope="sp_1", event=event)
    assert sent == []
    assert client._channel_space.get("ch_1") == "sp_1"


# ─── remove_from_channel / leave_channel ───────────────────────────


@pytest.mark.asyncio
async def test_remove_from_channel_evicts_caches_and_dms():
    client, sent = _make_client()
    client._channel_space["ch_priv"] = "sp_1"
    client._channel_name_cache["ch_priv"] = "secrets"

    event = {
        "kind": "remove_from_channel",
        "signer_slug": "alice-0001",
        "payload": {
            "space_id": "sp_1",
            "channel_id": "ch_priv",
            "removed_slug": "agent-1",
        },
    }
    await client._handle_event(scope="sp_1", event=event)

    assert "ch_priv" not in client._channel_space
    assert "ch_priv" not in client._channel_name_cache
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "secrets" in text
    assert "ch_priv" in text
    assert "Team" in text  # parent space label is included
    assert "Alice" in text


@pytest.mark.asyncio
async def test_leave_channel_self_evicts_caches_no_dm():
    """Voluntary channel exit signed by the agent itself: clean up
    caches but don't DM (operator-initiated, they already know)."""
    client, sent = _make_client()
    client._channel_space["ch_priv"] = "sp_1"
    client._channel_name_cache["ch_priv"] = "secrets"

    event = {
        "kind": "leave_channel",
        "signer_slug": "agent-1",
        "payload": {"space_id": "sp_1", "channel_id": "ch_priv"},
    }
    await client._handle_event(scope="sp_1", event=event)

    assert "ch_priv" not in client._channel_space
    assert "ch_priv" not in client._channel_name_cache
    assert sent == []


# ─── cancel_space_invite / cancel_channel_invite ───────────────────


@pytest.mark.asyncio
async def test_cancel_space_invite_dms_operator_when_dm_was_outstanding():
    """Operator was DM'd a y/n prompt for the invite; the invite is
    now withdrawn. Send a follow-up DM in the same thread so the
    operator doesn't reply ``y`` to nothing."""
    client, sent = _make_client()
    client._pending_invite_dms["env_invite_dm"] = {
        "kind": "invite_to_space",
        "invitation_event_id": "ev_invite_1",
        "inviter_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "",
        "space_name": "Team",
        "channel_name": None,
    }

    event = {
        "kind": "cancel_space_invite",
        "signer_slug": "alice-0001",
        "payload": {
            "space_id": "sp_1",
            "invitation_event_id": "ev_invite_1",
        },
    }
    await client._handle_event(scope="sp_1", event=event)

    # Outstanding DM is dropped; processed-set seeded so a stale
    # /invites poll can't re-fire.
    assert "env_invite_dm" not in client._pending_invite_dms
    assert "ev_invite_1" in client._processed_invite_ids

    # Follow-up DM threaded to the original prompt.
    assert len(sent) == 1
    assert sent[0]["to"] == "op-1"
    assert sent[0]["root_id"] == "env_invite_dm"
    assert "withdrew" in sent[0]["text"]
    assert "Team" in sent[0]["text"]


@pytest.mark.asyncio
async def test_cancel_channel_invite_dms_operator_when_dm_was_outstanding():
    """Same as the space cancel path but for an outstanding channel
    invite DM — the follow-up text references the channel label."""
    client, sent = _make_client()
    client._pending_invite_dms["env_chan_dm"] = {
        "kind": "invite_to_channel",
        "invitation_event_id": "ev_invite_2",
        "inviter_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_priv",
        "space_name": "Team",
        "channel_name": "secrets",
    }

    event = {
        "kind": "cancel_channel_invite",
        "signer_slug": "alice-0001",
        "payload": {
            "space_id": "sp_1",
            "channel_id": "ch_priv",
            "invitation_event_id": "ev_invite_2",
        },
    }
    await client._handle_event(scope="sp_1", event=event)

    assert "env_chan_dm" not in client._pending_invite_dms
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "secrets" in text
    assert "channel" in text


@pytest.mark.asyncio
async def test_cancel_invite_with_no_outstanding_dm_is_noop():
    """If the agent auto-accepted or never DM'd the operator, there's
    no outstanding y/n prompt — silently no-op rather than DMing a
    confusing "invite was withdrawn" with no prior context."""
    client, sent = _make_client()
    event = {
        "kind": "cancel_space_invite",
        "signer_slug": "alice-0001",
        "payload": {
            "space_id": "sp_1",
            "invitation_event_id": "ev_unknown",
        },
    }
    await client._handle_event(scope="sp_1", event=event)
    assert sent == []
    assert "ev_unknown" not in client._processed_invite_ids


# ─── operator_slug unset ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_membership_change_with_no_operator_slug_logs_but_doesnt_crash():
    """Early-provisioning agents have no operator_slug yet. Cache
    eviction must still happen; the DM is skipped without erroring."""
    client, sent = _make_client(operator_slug="")
    client._channel_space["ch_1"] = "sp_1"

    event = {
        "kind": "remove_from_space",
        "signer_slug": "alice-0001",
        "payload": {"space_id": "sp_1", "removed_slug": "agent-1"},
    }
    await client._handle_event(scope="sp_1", event=event)

    # Eviction still ran.
    assert "ch_1" not in client._channel_space
    # No DM attempted.
    assert sent == []
