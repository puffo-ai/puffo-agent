"""Per-agent bundle state machine.

Spec (corrected): messages are grouped by ``root_id``. Delivery is
ack-gated and serial — at most one bundle in flight per agent. A
bundle is **frozen on send**: while it's in flight, new arrivals on
the same root go into a *successor* receiving bundle, never into the
one the tool is acking. If the in-flight bundle is rolled back (tool
presumed dead), it merges with its successor into a single bundle and
re-enters the pending set under a fresh id, so a late ack of the old
id is safely ignored.

This module is pure and time-free: the session decides *when* to roll
back; the queue only knows *how*. Determinism makes it exhaustively
unit-testable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


def _default_id() -> str:
    return f"bdl_{uuid.uuid4().hex}"


@dataclass
class Bundle:
    bundle_id: str
    root_id: str
    channel_meta: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Arrival order of the bundle's first message; the send order and
    # the merge tie-breaker. Lower = older = sent first.
    order: int = 0

    def envelope_ids(self) -> list[str]:
        return [m.get("envelope_id", "") for m in self.messages]


class BundleQueue:
    def __init__(self, *, make_id: Callable[[], str] = _default_id) -> None:
        self._make_id = make_id
        self._seq = 0
        # One open bundle per root, accumulating arrivals.
        self._receiving: dict[str, Bundle] = {}
        self._inflight: Bundle | None = None

    # ── ingestion ────────────────────────────────────────────────────────────

    def enqueue(
        self,
        root_id: str,
        message: dict[str, Any],
        channel_meta: dict[str, Any] | None = None,
    ) -> None:
        """Route one decrypted message into its root's receiving bundle.

        If the root's bundle is currently in flight it was removed from
        ``_receiving`` on send, so this transparently opens the
        successor.
        """
        bundle = self._receiving.get(root_id)
        if bundle is None:
            bundle = Bundle(
                bundle_id=self._make_id(),
                root_id=root_id,
                channel_meta=dict(channel_meta or {}),
                order=self._next_seq(),
            )
            self._receiving[root_id] = bundle
        eid = message.get("envelope_id", "")
        if eid and eid in set(bundle.envelope_ids()):
            return
        bundle.messages.append(message)

    # ── delivery ─────────────────────────────────────────────────────────────

    def next_to_send(self) -> Bundle | None:
        """Freeze and return the oldest pending bundle, or None when one
        is already in flight or nothing is pending."""
        if self._inflight is not None:
            return None
        candidate: Bundle | None = None
        for bundle in self._receiving.values():
            if candidate is None or bundle.order < candidate.order:
                candidate = bundle
        if candidate is None:
            return None
        del self._receiving[candidate.root_id]
        self._inflight = candidate
        return candidate

    def ack(self, bundle_id: str) -> Bundle | None:
        """Confirm the in-flight bundle. Returns it so the caller can
        advance the cursor + processing-status. A stale/unknown id
        (double-ack, or an ack arriving after rollback) returns None."""
        if self._inflight is not None and self._inflight.bundle_id == bundle_id:
            done = self._inflight
            self._inflight = None
            return done
        return None

    def rollback_inflight(self) -> Bundle | None:
        """Return the in-flight bundle to pending, merging it with any
        successor on the same root. Assigns a fresh id so a late ack of
        the old id no-ops. Returns the resulting pending bundle (or None
        when nothing was in flight)."""
        if self._inflight is None:
            return None
        stale = self._inflight
        self._inflight = None
        successor = self._receiving.get(stale.root_id)
        merged = Bundle(
            bundle_id=self._make_id(),
            root_id=stale.root_id,
            channel_meta=stale.channel_meta or (successor.channel_meta if successor else {}),
            messages=list(stale.messages),
            order=stale.order,
        )
        if successor is not None:
            seen = set(merged.envelope_ids())
            for msg in successor.messages:
                if msg.get("envelope_id", "") not in seen:
                    merged.messages.append(msg)
        self._receiving[stale.root_id] = merged
        return merged

    # ── introspection (tests / metrics) ──────────────────────────────────────

    @property
    def inflight(self) -> Bundle | None:
        return self._inflight

    @property
    def has_inflight(self) -> bool:
        return self._inflight is not None

    def pending_count(self) -> int:
        # A receiving bundle always holds ≥1 message — it's created with
        # one and never drained in place.
        return len(self._receiving)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq
