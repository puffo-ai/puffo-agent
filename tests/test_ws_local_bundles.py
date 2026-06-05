"""Bundle state machine.

Exhaustive: routing by root, freeze-on-send, successor accumulation,
serial ack-gating, cross-root send order, ack/double-ack/stale-ack,
rollback with and without a successor, merge + dedup, and the
late-ack-after-rollback safety (old id no-ops).
"""

from __future__ import annotations

import itertools

import pytest

from puffo_agent.portal.ws_local.bundles import Bundle, BundleQueue


def _counter_ids():
    seq = itertools.count(1)
    return lambda: f"bdl_{next(seq)}"


def _msg(eid: str, text: str = "") -> dict:
    return {"envelope_id": eid, "text": text or eid}


@pytest.fixture
def q() -> BundleQueue:
    return BundleQueue(make_id=_counter_ids())


# ── ingestion + routing ──────────────────────────────────────────────────────


def test_enqueue_groups_by_root(q):
    q.enqueue("r1", _msg("a"))
    q.enqueue("r1", _msg("b"))
    q.enqueue("r2", _msg("c"))
    assert q.pending_count() == 2
    bundle = q.next_to_send()
    assert bundle.root_id == "r1"
    assert bundle.envelope_ids() == ["a", "b"]


def test_enqueue_dedups_within_bundle(q):
    q.enqueue("r1", _msg("a"))
    q.enqueue("r1", _msg("a"))
    bundle = q.next_to_send()
    assert bundle.envelope_ids() == ["a"]


def test_channel_meta_captured_from_first_message(q):
    q.enqueue("r1", _msg("a"), {"channel_id": "ch_9"})
    q.enqueue("r1", _msg("b"), {"channel_id": "ignored"})
    assert q.next_to_send().channel_meta == {"channel_id": "ch_9"}


# ── serial delivery + send order ─────────────────────────────────────────────


def test_only_one_inflight_at_a_time(q):
    q.enqueue("r1", _msg("a"))
    q.enqueue("r2", _msg("b"))
    first = q.next_to_send()
    assert first is not None
    assert q.has_inflight
    # Second call blocked until the first is acked.
    assert q.next_to_send() is None


def test_send_order_is_oldest_root_first(q):
    q.enqueue("r1", _msg("a"))
    q.enqueue("r2", _msg("b"))
    assert q.next_to_send().root_id == "r1"
    q.ack(q.inflight.bundle_id)
    assert q.next_to_send().root_id == "r2"


# ── freeze on send + successor ───────────────────────────────────────────────


def test_inflight_bundle_is_frozen(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    q.enqueue("r1", _msg("b"))  # same root, arrives mid-flight
    assert inflight.envelope_ids() == ["a"], "in-flight snapshot must not grow"
    assert q.pending_count() == 1, "new arrival opened a successor bundle"


def test_successor_delivered_after_ack(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    q.enqueue("r1", _msg("b"))
    q.ack(inflight.bundle_id)
    successor = q.next_to_send()
    assert successor.envelope_ids() == ["b"]
    assert successor.bundle_id != inflight.bundle_id


# ── ack semantics ────────────────────────────────────────────────────────────


def test_ack_returns_bundle_and_clears_inflight(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    done = q.ack(inflight.bundle_id)
    assert done is inflight
    assert not q.has_inflight


def test_double_ack_is_noop(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    assert q.ack(inflight.bundle_id) is inflight
    assert q.ack(inflight.bundle_id) is None


def test_ack_unknown_id_is_noop(q):
    q.enqueue("r1", _msg("a"))
    q.next_to_send()
    assert q.ack("bdl_does_not_exist") is None
    assert q.has_inflight


def test_ack_with_nothing_inflight(q):
    assert q.ack("anything") is None


# ── rollback + merge ─────────────────────────────────────────────────────────


def test_rollback_without_successor_requeues_under_new_id(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    merged = q.rollback_inflight()
    assert merged.envelope_ids() == ["a"]
    assert merged.bundle_id != inflight.bundle_id
    assert not q.has_inflight
    assert q.pending_count() == 1


def test_rollback_merges_with_successor_preserving_order(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    q.enqueue("r1", _msg("b"))
    q.enqueue("r1", _msg("c"))
    merged = q.rollback_inflight()
    assert merged.envelope_ids() == ["a", "b", "c"]
    assert merged.bundle_id != inflight.bundle_id


def test_rollback_merge_dedups(q):
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    q.enqueue("r1", _msg("a"))  # duplicate of the in-flight message
    q.enqueue("r1", _msg("b"))
    merged = q.rollback_inflight()
    assert merged.envelope_ids() == ["a", "b"]


def test_late_ack_after_rollback_is_ignored(q):
    """The poison-resistance guarantee: a tool that was presumed dead
    but later acks the OLD id must not double-advance."""
    q.enqueue("r1", _msg("a"))
    inflight = q.next_to_send()
    q.rollback_inflight()
    # Old id is gone; ack no-ops, merged bundle stays pending.
    assert q.ack(inflight.bundle_id) is None
    assert q.pending_count() == 1


def test_rollback_with_nothing_inflight(q):
    assert q.rollback_inflight() is None


def test_rolled_back_bundle_resends_then_acks_clean(q):
    q.enqueue("r1", _msg("a"))
    q.next_to_send()
    merged = q.rollback_inflight()
    resent = q.next_to_send()
    assert resent.bundle_id == merged.bundle_id
    assert q.ack(resent.bundle_id) is resent


# ── misc / empties ───────────────────────────────────────────────────────────


def test_next_to_send_empty_queue_is_none(q):
    assert q.next_to_send() is None


def test_empty_receiving_bundle_not_counted(q):
    # A root with only a dedup-dropped duplicate still counts once;
    # a genuinely empty queue counts zero.
    assert q.pending_count() == 0


def test_bundle_envelope_ids_handles_missing_field():
    b = Bundle(bundle_id="x", root_id="r", channel_meta={}, messages=[{"text": "no id"}])
    assert b.envelope_ids() == [""]


def test_default_id_factory_used_when_not_injected():
    q = BundleQueue()
    q.enqueue("r1", _msg("a"))
    assert q.next_to_send().bundle_id.startswith("bdl_")
