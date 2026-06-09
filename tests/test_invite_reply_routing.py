"""PUF-287: routing-gate relax so top-level (un-threaded) operator
Y/N on a pending invite-DM still triggers `_maybe_handle_invite_reply`.
PUF-227-A's threaded-fast-path is preserved as the precedence rule;
top-level falls back to single-pending lookup, multi-pending falls
through to the LLM.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _make_client(operator_slug: str = "op-1") -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client._pending_invite_dms = {}
    import logging
    client._log = logging.getLogger("puf287-test")

    accept_calls: list[tuple] = []
    reject_calls: list[tuple] = []

    async def _stub_accept(kind, eid, space_id, channel_id):
        accept_calls.append((kind, eid, space_id, channel_id))

    async def _stub_reject(kind, eid, space_id, channel_id):
        reject_calls.append((kind, eid, space_id, channel_id))

    async def _stub_send_dm(recipient_slug, text, root_id=""):
        return {"envelope_id": f"env_dm_{len(accept_calls)+len(reject_calls)}"}

    async def _stub_display_name(slug):
        return {"alice-0001": "Alice"}.get(slug, "")

    client._accept_invite = _stub_accept  # type: ignore[assignment]
    client._reject_invite = _stub_reject  # type: ignore[assignment]
    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]
    client._accept_calls = accept_calls  # type: ignore[attr-defined]
    client._reject_calls = reject_calls  # type: ignore[attr-defined]
    return client


def _seed_pending(client, env_id: str, **over) -> None:
    client._pending_invite_dms[env_id] = {
        "kind": over.get("kind", "invite_to_space"),
        "invitation_event_id": over.get("invitation_event_id", f"ev_{env_id}"),
        "inviter_slug": over.get("inviter_slug", "alice-0001"),
        "space_id": over.get("space_id", "sp_1"),
        "channel_id": over.get("channel_id", ""),
        "space_name": over.get("space_name", "Team"),
        "channel_name": over.get("channel_name", None),
    }


# ─── _resolve_invite_thread_root ───────────────────────────────────


def test_threaded_match_in_pending_wins_over_top_level_fallback():
    """PUF-227-A regression guard: when payload thread_root_id matches
    a registered invite, that's the resolver's answer regardless of
    pending-count.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_a")
    _seed_pending(client, "env_invite_b")

    resolved = client._resolve_invite_thread_root("env_invite_a", "y")
    assert resolved == "env_invite_a"


def test_top_level_y_with_one_pending_resolves_to_that_pending():
    client = _make_client()
    _seed_pending(client, "env_invite_solo")

    # Top-level: payload_thread_root_id is the message's own id, not
    # a registered invite root. Resolver should fall back to the
    # single-pending invite.
    resolved = client._resolve_invite_thread_root("env_top_level_msg", "y")
    assert resolved == "env_invite_solo"


def test_top_level_y_with_zero_pending_returns_none():
    client = _make_client()
    resolved = client._resolve_invite_thread_root("env_top_level_msg", "y")
    assert resolved is None


def test_resolver_handles_none_payload_thread_root_id():
    """The gate passes ``payload_thread_root_id`` straight in even
    when the WS frame omits it; resolver must treat ``None`` like a
    non-match and fall through to the single-pending path.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    resolved = client._resolve_invite_thread_root(None, "y")
    assert resolved == "env_invite_solo"


def test_top_level_y_with_multi_pending_returns_none():
    client = _make_client()
    _seed_pending(client, "env_invite_a")
    _seed_pending(client, "env_invite_b")
    resolved = client._resolve_invite_thread_root("env_top_level_msg", "y")
    assert resolved is None


def test_top_level_conversational_yes_does_not_resolve():
    """Strict-Y/N parser is the contract; "Yes, sure" should NOT
    trigger the routing intercept even with one pending invite.
    The match is reserved for the literal {y, yes, n, no} tokens.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    resolved = client._resolve_invite_thread_root("env_top_msg", "Yes, sure")
    assert resolved is None


@pytest.mark.parametrize("text", ["y", "Y", "yes", "YES", "  y  ", " no ", "N"])
def test_top_level_strict_yn_variants_resolve_to_single_pending(text):
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    resolved = client._resolve_invite_thread_root("env_top_msg", text)
    assert resolved == "env_invite_solo"


# ─── end-to-end through _maybe_handle_invite_reply ─────────────────


@pytest.mark.asyncio
async def test_top_level_y_with_one_pending_calls_accept():
    """PUF-287 primary fix: Shan's scenario. Operator replies "y"
    top-level (no thread); _accept_invite fires once and the pending
    entry clears.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_solo", invitation_event_id="ev_xyz")

    # Simulate the resolver+handler call the gate makes.
    resolved = client._resolve_invite_thread_root("env_top_msg", "y")
    assert resolved == "env_invite_solo"
    handled = await client._maybe_handle_invite_reply(
        thread_root_id=resolved, text="y",
    )

    assert handled is True
    assert client._accept_calls == [
        ("invite_to_space", "ev_xyz", "sp_1", "")
    ]
    assert "env_invite_solo" not in client._pending_invite_dms


@pytest.mark.asyncio
async def test_threaded_y_in_pending_root_still_works():
    """PUF-227-A regression: threaded path remains the primary fast
    route. payload_thread_root_id matches the registered invite root
    directly without going through the single-pending fallback.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_thr", invitation_event_id="ev_thr")

    resolved = client._resolve_invite_thread_root("env_invite_thr", "y")
    assert resolved == "env_invite_thr"
    handled = await client._maybe_handle_invite_reply(
        thread_root_id=resolved, text="y",
    )

    assert handled is True
    assert client._accept_calls == [
        ("invite_to_space", "ev_thr", "sp_1", "")
    ]


@pytest.mark.asyncio
async def test_top_level_n_with_one_pending_calls_reject():
    client = _make_client()
    _seed_pending(
        client, "env_invite_chan",
        kind="invite_to_channel",
        invitation_event_id="ev_chan",
        channel_id="ch_priv",
        channel_name="secrets",
    )

    resolved = client._resolve_invite_thread_root("env_top_msg", "n")
    handled = await client._maybe_handle_invite_reply(
        thread_root_id=resolved, text="n",
    )

    assert handled is True
    assert client._reject_calls == [
        ("invite_to_channel", "ev_chan", "sp_1", "ch_priv")
    ]


@pytest.mark.asyncio
async def test_top_level_y_with_zero_pending_no_op():
    """Operator types "y" at top-level with no pending invite — the
    resolver returns None and the gate falls through to the LLM
    queue. Verified here by asserting no accept/reject call.
    """
    client = _make_client()
    resolved = client._resolve_invite_thread_root("env_top_msg", "y")
    assert resolved is None
    assert client._accept_calls == []
    assert client._reject_calls == []


@pytest.mark.asyncio
async def test_new_invite_seeded_between_awaits_does_not_corrupt_accept():
    """PUF-287 race scenario: a fresh invite registers in
    ``_pending_invite_dms`` while ``_maybe_handle_invite_reply`` is
    mid-await on ``_accept_invite``. asyncio's single-threaded model
    means the resolver's pending-id snapshot is taken before the
    handler awaits, so a concurrent insert can't redirect this y/n.
    Locks the contract: the resolved root is consumed exactly once
    and the late-arriving invite stays untouched.
    """
    client = _make_client()
    _seed_pending(client, "env_invite_first", invitation_event_id="ev_first")
    late_arrived = {"slug": False}

    async def _stub_accept_with_late_seed(kind, eid, space_id, channel_id):
        # Simulate a second invite registering mid-accept.
        _seed_pending(
            client, "env_invite_late",
            invitation_event_id="ev_late",
        )
        late_arrived["slug"] = True
        # Original stub records the call.
        client._accept_calls.append((kind, eid, space_id, channel_id))

    client._accept_invite = _stub_accept_with_late_seed  # type: ignore[assignment]

    resolved = client._resolve_invite_thread_root("env_top_msg", "y")
    handled = await client._maybe_handle_invite_reply(
        thread_root_id=resolved, text="y",
    )

    assert handled is True
    assert late_arrived["slug"] is True
    # First invite consumed; late invite still pending and untouched.
    assert client._accept_calls == [("invite_to_space", "ev_first", "sp_1", "")]
    assert "env_invite_first" not in client._pending_invite_dms
    assert "env_invite_late" in client._pending_invite_dms


@pytest.mark.asyncio
async def test_top_level_y_with_multi_pending_logs_and_falls_through(caplog):
    """Multi-pending v1 behavior: ambiguous, falls to LLM with a
    debug-log. Operator can disambiguate via natural language or
    re-issue in-thread.
    """
    import logging
    client = _make_client()
    _seed_pending(client, "env_invite_a")
    _seed_pending(client, "env_invite_b")

    with caplog.at_level(logging.INFO, logger="puf287-test"):
        resolved = client._resolve_invite_thread_root("env_top_msg", "y")

    assert resolved is None
    matched = [r for r in caplog.records if "ambiguous" in r.getMessage()]
    assert len(matched) == 1
    assert "2 pending invites" in matched[0].getMessage()


# ─── (β) UX copy ───────────────────────────────────────────────────


def test_invite_prompt_copy_mentions_top_level_path():
    """PUF-287 (β): the operator-facing invite prompt must surface
    that top-level replies are accepted, not just threaded ones.
    Reads the source so the test fails the moment a refactor reverts
    the wording.
    """
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "puffo_agent",
        "agent", "puffo_core_client.py",
    )
    with open(src_path, "r", encoding="utf-8") as fh:
        body = fh.read()
    # Both invite_to_space + invite_to_channel branches carry the
    # phrase; assert it appears at least twice. The wording crosses
    # an adjacent-string boundary in source so we anchor on the
    # contiguous "as a direct reply" tail.
    assert body.count("or as a direct reply") >= 2
