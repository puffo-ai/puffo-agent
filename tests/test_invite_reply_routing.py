"""Operator Y/N routing on pending invite-DMs. A threaded reply answers
just that invite; a direct (top-level) Y/N answers all the operator's
pending invites, with a consolidated summary posted back in the direct
reply's own thread.
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
    client._log = logging.getLogger("invite-routing-test")

    accept_calls: list[tuple] = []
    reject_calls: list[tuple] = []
    sent_dms: list[dict] = []

    async def _stub_accept(kind, eid, space_id, channel_id):
        accept_calls.append((kind, eid, space_id, channel_id))

    async def _stub_reject(kind, eid, space_id, channel_id):
        reject_calls.append((kind, eid, space_id, channel_id))

    async def _stub_send_dm(recipient_slug, text, root_id=""):
        sent_dms.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_dm_{len(sent_dms)}"}

    async def _stub_display_name(slug):
        return {"alice-0001": "Alice"}.get(slug, "")

    client._accept_invite = _stub_accept  # type: ignore[assignment]
    client._reject_invite = _stub_reject  # type: ignore[assignment]
    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]
    client._accept_calls = accept_calls  # type: ignore[attr-defined]
    client._reject_calls = reject_calls  # type: ignore[attr-defined]
    client._sent_dms = sent_dms  # type: ignore[attr-defined]
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


# ─── _resolve_invite_targets (routing) ─────────────────────────────


def test_threaded_match_wins_and_is_not_direct():
    """Regression guard: a threaded reply matching a registered invite
    targets just that one (and isn't treated as a direct reply)."""
    client = _make_client()
    _seed_pending(client, "env_invite_a")
    _seed_pending(client, "env_invite_b")
    roots, is_direct = client._resolve_invite_targets("env_invite_a", "y")
    assert roots == ["env_invite_a"]
    assert is_direct is False


def test_direct_y_single_pending_targets_it():
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    roots, is_direct = client._resolve_invite_targets("env_top_level_msg", "y")
    assert roots == ["env_invite_solo"]
    assert is_direct is True


def test_direct_y_multi_pending_targets_all():
    client = _make_client()
    _seed_pending(client, "env_invite_a")
    _seed_pending(client, "env_invite_b")
    roots, is_direct = client._resolve_invite_targets("env_top_level_msg", "y")
    assert set(roots) == {"env_invite_a", "env_invite_b"}
    assert is_direct is True


def test_direct_y_zero_pending_is_empty():
    client = _make_client()
    roots, is_direct = client._resolve_invite_targets("env_top_level_msg", "y")
    assert roots == []
    assert is_direct is True


def test_resolver_handles_none_thread_root():
    """The gate passes ``payload_thread_root_id`` straight in even when
    the WS frame omits it; ``None`` is a non-match → direct path."""
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    roots, is_direct = client._resolve_invite_targets(None, "y")
    assert roots == ["env_invite_solo"]
    assert is_direct is True


def test_conversational_yes_does_not_route():
    """Strict-Y/N is the contract; "Yes, sure" must not route even with
    a pending invite."""
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    roots, is_direct = client._resolve_invite_targets("env_top_msg", "Yes, sure")
    assert roots == []
    assert is_direct is False


@pytest.mark.parametrize("text", ["y", "Y", "yes", "YES", "  y  ", " no ", "N"])
def test_strict_yn_variants_route(text):
    client = _make_client()
    _seed_pending(client, "env_invite_solo")
    roots, is_direct = client._resolve_invite_targets("env_top_msg", text)
    assert roots == ["env_invite_solo"]
    assert is_direct is True


# ─── _apply_invite_replies (accept / reject each) ──────────────────


@pytest.mark.asyncio
async def test_direct_y_single_accepts_and_clears():
    client = _make_client()
    _seed_pending(client, "env_invite_solo", invitation_event_id="ev_xyz")
    roots, _ = client._resolve_invite_targets("env_top_msg", "y")
    labels = await client._apply_invite_replies(roots, "y")
    assert labels == ["space **Team**"]
    assert client._accept_calls == [("invite_to_space", "ev_xyz", "sp_1", "")]
    assert "env_invite_solo" not in client._pending_invite_dms


@pytest.mark.asyncio
async def test_threaded_y_accepts_only_that_one():
    """The threaded path answers a single invite even when others are
    pending — it isn't a bulk action."""
    client = _make_client()
    _seed_pending(client, "env_invite_thr", invitation_event_id="ev_thr")
    _seed_pending(client, "env_invite_other", invitation_event_id="ev_other")
    roots, is_direct = client._resolve_invite_targets("env_invite_thr", "y")
    assert is_direct is False
    await client._apply_invite_replies(roots, "y")
    assert client._accept_calls == [("invite_to_space", "ev_thr", "sp_1", "")]
    assert "env_invite_other" in client._pending_invite_dms


@pytest.mark.asyncio
async def test_direct_y_multi_accepts_all():
    client = _make_client()
    _seed_pending(client, "env_a", invitation_event_id="ev_a", space_name="Alpha")
    _seed_pending(client, "env_b", invitation_event_id="ev_b", space_name="Beta")
    roots, _ = client._resolve_invite_targets("env_top_msg", "y")
    labels = await client._apply_invite_replies(roots, "y")
    assert {c[1] for c in client._accept_calls} == {"ev_a", "ev_b"}
    assert set(labels) == {"space **Alpha**", "space **Beta**"}
    assert client._pending_invite_dms == {}


@pytest.mark.asyncio
async def test_direct_n_multi_rejects_all():
    client = _make_client()
    _seed_pending(client, "env_a", invitation_event_id="ev_a")
    _seed_pending(client, "env_b", invitation_event_id="ev_b")
    roots, _ = client._resolve_invite_targets("env_top_msg", "n")
    await client._apply_invite_replies(roots, "n")
    assert {c[1] for c in client._reject_calls} == {"ev_a", "ev_b"}
    assert client._accept_calls == []


@pytest.mark.asyncio
async def test_direct_y_zero_pending_no_op():
    client = _make_client()
    roots, _ = client._resolve_invite_targets("env_top_msg", "y")
    labels = await client._apply_invite_replies(roots, "y")
    assert roots == [] and labels == []
    assert client._accept_calls == []


@pytest.mark.asyncio
async def test_targets_snapshot_excludes_mid_accept_seed():
    """A fresh invite registering mid-accept isn't in the already-
    resolved target list, so a direct y/n can't sweep it up."""
    client = _make_client()
    _seed_pending(client, "env_first", invitation_event_id="ev_first")

    async def _accept_late_seed(kind, eid, space_id, channel_id):
        _seed_pending(client, "env_late", invitation_event_id="ev_late")
        client._accept_calls.append((kind, eid, space_id, channel_id))

    client._accept_invite = _accept_late_seed  # type: ignore[assignment]
    roots, _ = client._resolve_invite_targets("env_top_msg", "y")
    await client._apply_invite_replies(roots, "y")
    assert client._accept_calls == [("invite_to_space", "ev_first", "sp_1", "")]
    assert "env_late" in client._pending_invite_dms


# ─── _send_invite_bulk_summary (summary in the direct reply thread) ─


@pytest.mark.asyncio
async def test_bulk_summary_threaded_under_direct_reply():
    client = _make_client()
    await client._send_invite_bulk_summary(
        ["space **Alpha**", "space **Beta**"], "y", "env_y_msg",
    )
    assert len(client._sent_dms) == 1
    dm = client._sent_dms[0]
    assert dm["root_id"] == "env_y_msg"  # threaded under the operator's y
    assert dm["to"] == "op-1"
    assert "2 invites" in dm["text"]
    assert "Alpha" in dm["text"] and "Beta" in dm["text"]
    assert dm["text"].endswith("✓")  # accept mark


@pytest.mark.asyncio
async def test_bulk_summary_single_is_singular():
    client = _make_client()
    await client._send_invite_bulk_summary(["space **Solo**"], "y", "env_y")
    assert client._sent_dms[0]["text"].startswith("Accepted invite to space **Solo**")


@pytest.mark.asyncio
async def test_bulk_summary_reject_has_no_check():
    client = _make_client()
    await client._send_invite_bulk_summary(["space **X**"], "n", "env_y")
    txt = client._sent_dms[0]["text"]
    assert txt.startswith("Rejected")
    assert "✓" not in txt


@pytest.mark.asyncio
async def test_gate_flow_direct_multi_accepts_all_then_summarizes():
    """The gate's direct path end-to-end: resolve → apply → one summary
    threaded under the operator's y (on top of the per-invite confirms).
    """
    client = _make_client()
    _seed_pending(client, "env_a", invitation_event_id="ev_a", space_name="Alpha")
    _seed_pending(client, "env_b", invitation_event_id="ev_b", space_name="Beta")
    roots, is_direct = client._resolve_invite_targets("env_y_msg", "y")
    labels = await client._apply_invite_replies(roots, "y")
    if is_direct:
        await client._send_invite_bulk_summary(labels, "y", "env_y_msg")
    assert {c[1] for c in client._accept_calls} == {"ev_a", "ev_b"}
    summaries = [d for d in client._sent_dms if d["root_id"] == "env_y_msg"]
    assert len(summaries) == 1
    assert "2 invites" in summaries[0]["text"]


# ─── (β) UX copy ───────────────────────────────────────────────────


def test_invite_prompt_copy_mentions_direct_all_pending():
    """The invite prompt must tell the operator a direct reply answers
    all pending invites. Reads the source so a reverted wording fails."""
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "puffo_agent",
        "agent", "puffo_core_client.py",
    )
    with open(src_path, "r", encoding="utf-8") as fh:
        body = fh.read()
    # Space + channel branches share one format_permission_prompt call,
    # so the bulk-reply note appears once and covers both.
    assert "pending invites at once" in body
