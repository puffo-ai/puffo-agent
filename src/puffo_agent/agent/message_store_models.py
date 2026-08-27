"""Shared value types and constants for the durable message store."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

# Supplementary context is deliberately smaller than a content-bearing Inbox
# page.  The runtime applies the formatted-byte guard as well; these bounds
# keep the durable lookup itself finite before formatting adds metadata.
PRIOR_CONTEXT_MAX_ITEMS = 20
PRIOR_CONTEXT_MAX_BYTES = 48_000
MAX_REMINDER_ENVELOPE_BYTES = 32_768

# Persisting an envelope is the write-ahead fence for remote delivery. Only an
# entirely unprepared row can use the local-only path. A remotely coordinated
# row additionally needs a freshly validated Server lease before delivery.
_REMINDER_LOCAL_DELIVERY_SQL = """
(
    snapshot_conflict = 0
    AND server_ack_revision = 0
    AND payload_format IS NULL
    AND opaque_payload IS NULL
    AND delivery_claim_id IS NULL
    AND delivery_claim_acquired = 0
    AND delivery_claim_expires_at_ms IS NULL
)
"""


def _reminder_delivery_custody_sql(now_parameter: str = "?") -> str:
    return f"""
    (
        snapshot_conflict = 0
        AND (
            {_REMINDER_LOCAL_DELIVERY_SQL}
            OR (
                delivery_claim_acquired = 1
                AND delivery_claim_id IS NOT NULL
                AND delivery_claim_expires_at_ms IS NOT NULL
                AND delivery_claim_expires_at_ms > {now_parameter}
            )
        )
    )
    """


def _now_ms() -> int:
    return int(time.time() * 1000)


class DataNotFound(Exception):
    """Raised by reads that need to distinguish "this channel /
    thread has never been seen" from "seen, but the requested
    window is empty after filters". The MCP tool layer surfaces a
    different user-facing message for each.
    """


class DataUnavailable(RuntimeError):
    """Raised when a read could not be answered at all — the data
    service errored or was unreachable. Distinct from an empty
    result: rendering a failure as "no messages" would hand the
    model a confidently wrong fact with no error trace.
    """


class ReceiptDisposition(str, Enum):
    ELIGIBLE = "eligible"
    TERMINAL = "terminal"
    FOREIGN_DM_GATED = "foreign_dm_gated"
    LOCAL_RUNTIME = "local_runtime"


class ProcessingState(str, Enum):
    PENDING = "pending"
    IN_TURN = "in_turn"
    PROCESSED = "processed"


class ReceiptWriteStatus(str, Enum):
    COMMITTED = "committed"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ReceiptResult:
    status: ReceiptWriteStatus
    disposition: ReceiptDisposition
    reason: str
    acknowledge: bool


@dataclass(frozen=True)
class PendingPage:
    items: tuple[StoredMessage, ...]
    through_seq: int | None
    more_available: bool


@dataclass(frozen=True)
class InboxNoticeState:
    generation: int
    pending_count: int
    first_pending_deadline_ms: int | None
    last_delivered_generation: int
    last_delivered_provider_session_id: str | None

    @property
    def delivery_pending(self) -> bool:
        """Whether the current generation lacks a session-aware delivery."""
        return self.pending_count > 0 and (
            self.generation != self.last_delivered_generation
            or not self.last_delivered_provider_session_id
        )

    def is_due_for(self, provider_session_id: str | None) -> bool:
        """Return whether this provider session needs Inbox discovery."""
        if self.pending_count <= 0:
            return False
        if not provider_session_id:
            return self.delivery_pending
        return (
            self.generation != self.last_delivered_generation
            or provider_session_id != self.last_delivered_provider_session_id
        )


@dataclass(frozen=True)
class InboxPage:
    items: tuple[StoredMessage, ...]
    next_cursor: str
    has_more: bool
    remaining_count: int
    snapshot_generation: int
    target: str


@dataclass(frozen=True)
class PriorContextPage:
    items: tuple[StoredMessage, ...]
    has_more: bool


@dataclass(frozen=True)
class TurnRun:
    turn_id: str
    provider_session_id: str | None
    state: str
    message_ids: tuple[str, ...]
    started_at: int
    completed_at: int | None


@dataclass(frozen=True)
class ReminderOccurrence:
    """The durable Agent-owned fact for one single-fire reminder."""

    reminder_id: str
    occurrence_id: str
    target: str
    content: str
    intended_at_ms: int
    state: str
    created_at_ms: int
    claimed_at_ms: int | None
    actual_fire_at_ms: int | None
    cancelled_at_ms: int | None
    delivered_at_ms: int | None
    delivered_event_id: str | None

    def as_dict(self) -> dict[str, Any]:
        """The stable, provider-neutral reminder tool result."""
        is_internal_claim = self.state == "claimed"
        return {
            "reminder_id": self.reminder_id,
            "occurrence_id": self.occurrence_id,
            "state": "scheduled" if is_internal_claim else self.state,
            "target": self.target,
            "content": self.content,
            "intended_at": reminder_time_to_rfc3339(self.intended_at_ms),
            "actual_fire_at": (
                reminder_time_to_rfc3339(self.actual_fire_at_ms)
                if self.actual_fire_at_ms is not None and not is_internal_claim
                else None
            ),
            "created_at": reminder_time_to_rfc3339(self.created_at_ms),
            "cancelled_at": (
                reminder_time_to_rfc3339(self.cancelled_at_ms)
                if self.cancelled_at_ms is not None
                else None
            ),
            "delivered_at": (
                reminder_time_to_rfc3339(self.delivered_at_ms)
                if self.delivered_at_ms is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ReminderSyncRecord:
    """Private sync projection of one durable reminder occurrence.

    This type is deliberately not used by MCP/RPC reminder results.  It
    contains local plaintext only long enough for the Agent-owned AEAD seam
    to construct (or validate) the persisted opaque envelope.
    """

    reminder_id: str
    occurrence_id: str
    target: str
    content: str
    intended_at_ms: int
    state: str
    created_at_ms: int
    claimed_at_ms: int | None
    actual_fire_at_ms: int | None
    cancelled_at_ms: int | None
    delivered_at_ms: int | None
    delivered_event_id: str | None
    revision: int
    server_ack_revision: int
    payload_format: str | None
    opaque_payload: bytes | None
    sync_retry_after_ms: int | None
    sync_retry_count: int
    sync_permanent_revision: int | None
    sync_permanent_code: str | None
    delivery_claim_id: str | None
    delivery_claim_acquired: bool
    delivery_claim_expires_at_ms: int | None
    snapshot_conflict: bool

    @property
    def is_unprepared_local_only(self) -> bool:
        """Whether this row has no evidence of remote delivery custody."""
        return (
            not self.snapshot_conflict
            and self.server_ack_revision == 0
            and self.payload_format is None
            and self.opaque_payload is None
            and self.delivery_claim_id is None
            and not self.delivery_claim_acquired
            and self.delivery_claim_expires_at_ms is None
        )

    @property
    def server_lifecycle(self) -> str:
        """Map local-only crash recovery state to the Server contract."""
        return "scheduled" if self.state == "claimed" else self.state

    @property
    def lifecycle_at_ms(self) -> int:
        if self.state in {"scheduled", "claimed"}:
            return self.created_at_ms
        if self.state == "cancelled" and self.cancelled_at_ms is not None:
            return self.cancelled_at_ms
        if self.state == "delivered" and self.delivered_at_ms is not None:
            return self.delivered_at_ms
        raise LifecycleConflict("reminder lifecycle timestamp is missing")


@dataclass(frozen=True)
class ReminderMaterializationResult:
    """Outcome of applying one validated Server occurrence locally."""

    changed: bool
    conflict: bool = False


REMINDER_STATES = frozenset({"scheduled", "claimed", "cancelled", "delivered"})
PUBLIC_REMINDER_STATES = frozenset({"scheduled", "cancelled", "delivered"})
MAX_REMINDER_LIST_LIMIT = 100


def reminder_plaintext_envelope_size(target: str, content: str) -> int:
    """Return the exact byte size of the deterministic v1 envelope shape."""

    plaintext = json.dumps(
        {"content": content, "target": target},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext_bytes = len(plaintext) + 16
    encoded_bytes = ((ciphertext_bytes + 2) // 3) * 4
    encoded_bytes -= (3 - ciphertext_bytes % 3) % 3
    envelope = json.dumps(
        {
            "algorithm": "chacha20-poly1305",
            "ciphertext": "A" * encoded_bytes,
            "nonce": "A" * 16,
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(envelope)


def reminder_time_to_rfc3339(value: int) -> str:
    """Render epoch milliseconds in one canonical UTC representation."""
    return (
        datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_inbox_target(target: str) -> tuple[str, str, str, str]:
    """Validate and decompose a canonical local Inbox target.

    Returns ``(kind, space_id, channel_id, thread_root_id_or_peer)``.  The
    one-shot contract deliberately accepts only the target forms which the
    Inbox already projects; it does not infer a provider route from prose.
    """
    if not isinstance(target, str) or not target or target != target.strip():
        raise ValueError("target must be a non-empty canonical Inbox target")
    parts = target.split(":")
    valid_segment = lambda value: bool(value) and value == value.strip()
    if parts[0] == "dm" and len(parts) == 2 and valid_segment(parts[1]):
        return ("dm", "", "", parts[1])
    if (
        parts[0] == "channel"
        and len(parts) == 3
        and valid_segment(parts[1])
        and valid_segment(parts[2])
    ):
        return ("channel", parts[1], parts[2], "")
    if (
        parts[0] == "channel"
        and len(parts) == 5
        and valid_segment(parts[1])
        and valid_segment(parts[2])
        and parts[3] == "thread"
        and valid_segment(parts[4])
    ):
        return ("channel", parts[1], parts[2], parts[4])
    raise ValueError(
        "target must be dm:<peer>, channel:<space_id>:<channel_id>, or "
        "channel:<space_id>:<channel_id>:thread:<root_id>"
    )


def parse_reminder_target(target: str) -> tuple[str, str, str, str]:
    """Backward-compatible reminder name for the shared target parser."""
    return parse_inbox_target(target)


class LifecycleConflict(Exception):
    """An exact Inbox lifecycle transition could not be applied."""


@dataclass
class StoredMessage:
    envelope_id: str
    envelope_kind: str
    sender_slug: str
    channel_id: Optional[str]
    space_id: Optional[str]
    recipient_slug: Optional[str]
    content_type: str
    content: Any
    sent_at: int
    received_at: int
    thread_root_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    # True when the claimed thread root was not locally verifiable at
    # receipt time (root not in the local store); grouping still uses the
    # claimed id, and arrival of the root later settles the flag.
    thread_root_unverified: bool = False
    # False only for a plaintext (non-E2EE) message; absent/legacy rows are True.
    is_encrypted: bool = True
    server_seq: int | None = None
    receipt_disposition: ReceiptDisposition | None = None
    receipt_reason: str | None = None
    processing_state: ProcessingState | None = None
    processing_turn_id: str | None = None
    model_visible_at: int | None = None
    processed_at: int | None = None
    local_ordinal: int | None = None
    after_server_seq: int | None = None
    # True once this row has been redelivered as an uncovered message; a
    # row is redelivered at most once, so this bit terminates the cycle.
    renotified: bool = False


@dataclass
class ChannelRoot:
    """One root post in a channel plus how many replies it accrued.
    Used by ``get_channel_roots`` to surface thread heads without
    blasting every reply into the agent's context window — the agent
    sees N=reply_count and calls ``get_thread_messages`` only on
    threads it actually wants to read into.
    """

    message: StoredMessage
    reply_count: int
