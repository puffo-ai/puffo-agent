"""Single-active-WS-per-identity registry.

Covers acquire/reject, release + takeover, the eviction-safety rule
(a session that lost the slot can't evict the winner), and the
atomic check-and-set that makes concurrent connects safe.
"""

from __future__ import annotations

from puffo_agent.portal.ws_local.registry import SessionRegistry


def test_first_acquire_wins_second_rejected():
    reg: SessionRegistry[str] = SessionRegistry()
    assert reg.acquire("alice", "sess_a") is True
    assert reg.acquire("alice", "sess_b") is False
    assert reg.current("alice") == "sess_a"


def test_release_frees_slot_for_takeover():
    reg: SessionRegistry[str] = SessionRegistry()
    reg.acquire("alice", "sess_a")
    reg.release("alice", "sess_a")
    assert reg.current("alice") is None
    assert reg.acquire("alice", "sess_b") is True


def test_release_by_non_owner_does_not_evict_winner():
    """After a takeover, the dead session's late ``release`` must not
    remove the new owner."""
    reg: SessionRegistry[str] = SessionRegistry()
    reg.acquire("alice", "sess_a")
    reg.release("alice", "sess_a")
    reg.acquire("alice", "sess_b")
    reg.release("alice", "sess_a")  # stale release from the old session
    assert reg.current("alice") == "sess_b"


def test_distinct_slugs_are_independent():
    reg: SessionRegistry[str] = SessionRegistry()
    assert reg.acquire("alice", "a") is True
    assert reg.acquire("bob", "b") is True
    assert reg.active_count() == 2


def test_release_unknown_slug_is_noop():
    reg: SessionRegistry[str] = SessionRegistry()
    reg.release("nobody", "x")
    assert reg.active_count() == 0


def test_release_is_idempotent():
    reg: SessionRegistry[str] = SessionRegistry()
    reg.acquire("alice", "a")
    reg.release("alice", "a")
    reg.release("alice", "a")
    assert reg.current("alice") is None
