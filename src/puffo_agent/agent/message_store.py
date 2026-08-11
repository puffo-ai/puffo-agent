from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import weakref
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ..portal.state import home_dir
from . import message_store_models as _models
from .inbox_store import InboxStoreMixin
from .reminder_store import ReminderStoreMixin

# Stable facade: callers continue importing store value types from this module
# while their implementations are shared by the focused store mixins.
PRIOR_CONTEXT_MAX_ITEMS = _models.PRIOR_CONTEXT_MAX_ITEMS
PRIOR_CONTEXT_MAX_BYTES = _models.PRIOR_CONTEXT_MAX_BYTES
DataNotFound = _models.DataNotFound
ReceiptDisposition = _models.ReceiptDisposition
ProcessingState = _models.ProcessingState
ReceiptWriteStatus = _models.ReceiptWriteStatus
ReceiptResult = _models.ReceiptResult
PendingPage = _models.PendingPage
InboxNoticeState = _models.InboxNoticeState
InboxPage = _models.InboxPage
PriorContextPage = _models.PriorContextPage
TurnRun = _models.TurnRun
ReminderOccurrence = _models.ReminderOccurrence
ReminderSyncRecord = _models.ReminderSyncRecord
ReminderMaterializationResult = _models.ReminderMaterializationResult
REMINDER_STATES = _models.REMINDER_STATES
MAX_REMINDER_LIST_LIMIT = _models.MAX_REMINDER_LIST_LIMIT
MAX_REMINDER_ENVELOPE_BYTES = _models.MAX_REMINDER_ENVELOPE_BYTES
reminder_plaintext_envelope_size = _models.reminder_plaintext_envelope_size
reminder_time_to_rfc3339 = _models.reminder_time_to_rfc3339
parse_reminder_target = _models.parse_reminder_target
LifecycleConflict = _models.LifecycleConflict
StoredMessage = _models.StoredMessage
ChannelRoot = _models.ChannelRoot
_now_ms = _models._now_ms

_SCHEMA_INIT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()

# Stored in place of a denied stranger's body once the operator answers "n".
# The row stays so envelope accounting and redelivery dedupe still work; the
# plaintext the operator refused does not.
DENIED_DM_PLACEHOLDER = "[message from a denied sender was discarded]"
_CONTENT_JSON_PREFIX = "\x1ePUFFO_CONTENT_JSON_V1:"

# A held foreign DM is withheld from the model on the push path, so every
# pull-side read must withhold it too. Applied to the DM, thread, and
# single-envelope model-visible reads — the channel reads cannot reach a
# gated row, which is always a DM. Runtime-internal envelope reads
# (redelivery dedupe, gate promotion, held/admission bookkeeping) stay
# deliberately unfiltered: they exist to act on the held row.
_NOT_GATED = (
    "(receipt_disposition IS NULL OR receipt_disposition != 'foreign_dm_gated')"
)


def _schema_init_lock(db_path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _SCHEMA_INIT_LOCKS.setdefault(loop, {})
    return locks.setdefault(str(db_path.resolve()), asyncio.Lock())


def _encode_content(value: Any) -> str:
    return _CONTENT_JSON_PREFIX + json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )


def _decode_content(
    value: Any,
    *,
    content_type: str,
    receipt_disposition: str | None,
) -> Any:
    raw = str(value or "")
    if raw.startswith(_CONTENT_JSON_PREFIX):
        try:
            return json.loads(raw[len(_CONTENT_JSON_PREFIX):])
        except (json.JSONDecodeError, ValueError):
            return raw
    # Before the explicit marker, structured local/eligible rows were stored
    # as bare JSON. Terminal remote prose is deliberately excluded: a user
    # message that happens to be valid JSON must remain text, not metadata.
    legacy_structured = (
        content_type.endswith("+json")
        or content_type == "application/json"
        or content_type == "puffo/message+attachments/v1"
        or receipt_disposition
        in {
            ReceiptDisposition.ELIGIBLE.value,
            ReceiptDisposition.LOCAL_RUNTIME.value,
        }
    )
    if legacy_structured:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    envelope_id TEXT PRIMARY KEY,
    envelope_kind TEXT NOT NULL,
    sender_slug TEXT NOT NULL,
    channel_id TEXT,
    space_id TEXT,
    recipient_slug TEXT,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    content TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    thread_root_id TEXT,
    reply_to_id TEXT,
    is_encrypted INTEGER NOT NULL DEFAULT 1
);

-- One row per channel the agent has been auto-prompted to introduce
-- itself in. Gate set in ``_accept_invite`` so a daemon restart (or a
-- server-side invite redelivery) can't trigger a second intro.
CREATE TABLE IF NOT EXISTS channel_intro_prompted (
    channel_id TEXT PRIMARY KEY,
    prompted_at INTEGER NOT NULL
);

-- Out-of-band channel→space mappings discovered without an inbound
-- message. The /messages table inference is enough for channels the
-- agent has received traffic from, but the intro-nudge path needs
-- the mapping BEFORE the first real message — agent calls
-- send_message against the freshly-joined channel, MCP asks
-- lookup_channel_space, and without this table the daemon-side
-- query returns 404 → MCP falls back to agent.yml's home space,
-- which is the wrong space when the agent is now multi-space.
-- Populated by ``_find_public_general_channel`` (and any future
-- channel-discovery hook). ``lookup_channel_space`` checks this
-- table first before the /messages fallback.
CREATE TABLE IF NOT EXISTS channel_space_map (
    channel_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    learned_at INTEGER NOT NULL
);

-- Last "FYI, X is DMing me" notice per foreign sender, so the 72h
-- throttle survives daemon restarts.
CREATE TABLE IF NOT EXISTS dm_notices (
    sender_slug TEXT PRIMARY KEY,
    last_notified_at INTEGER NOT NULL
);
"""

_DEPENDENT_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_messages_channel
    ON messages (channel_id, sent_at) WHERE channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_dm
    ON messages (sender_slug, sent_at) WHERE envelope_kind = 'dm';
CREATE INDEX IF NOT EXISTS idx_messages_dm_recipient
    ON messages (recipient_slug, sent_at) WHERE envelope_kind = 'dm';
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages (received_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread_root
    ON messages (thread_root_id, sent_at) WHERE thread_root_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_server_seq_unique
    ON messages (server_seq) WHERE server_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_pending_fifo
    ON messages (processing_state, server_seq, after_server_seq, local_ordinal);
CREATE INDEX IF NOT EXISTS idx_messages_turn
    ON messages (processing_turn_id, processing_state);
CREATE INDEX IF NOT EXISTS idx_messages_channel_pending
    ON messages (space_id, channel_id, processing_state, server_seq);

CREATE TABLE IF NOT EXISTS channel_context_state (
    space_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    context_baseline_seq INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (space_id, channel_id)
);
CREATE TABLE IF NOT EXISTS turn_runs (
    turn_id TEXT PRIMARY KEY,
    provider_session_id TEXT,
    state TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS turn_run_messages (
    turn_id TEXT NOT NULL,
    envelope_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (turn_id, envelope_id),
    UNIQUE (turn_id, ordinal),
    FOREIGN KEY (turn_id) REFERENCES turn_runs(turn_id)
);
CREATE TABLE IF NOT EXISTS local_ordinal_allocator (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    next_ordinal INTEGER NOT NULL
);
INSERT OR IGNORE INTO local_ordinal_allocator(singleton, next_ordinal) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS server_sequence_high_water (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    high_water INTEGER NOT NULL CHECK (high_water >= 0)
);
INSERT OR IGNORE INTO server_sequence_high_water(singleton, high_water) VALUES (1, 0);
UPDATE server_sequence_high_water
SET high_water = MAX(
    high_water,
    COALESCE((SELECT MAX(server_seq) FROM messages), 0)
)
WHERE singleton = 1;
CREATE TABLE IF NOT EXISTS inbox_notice_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    first_pending_deadline_ms INTEGER,
    last_delivered_generation INTEGER NOT NULL,
    last_delivered_provider_session_id TEXT
);
INSERT OR IGNORE INTO inbox_notice_state(
    singleton, generation, pending_count, first_pending_deadline_ms,
    last_delivered_generation
) VALUES (1, 0, 0, NULL, 0);

-- One row is both the immutable one-shot reminder intent and its only
-- occurrence. This table intentionally represents a single trigger only.
CREATE TABLE IF NOT EXISTS reminder_occurrences (
    reminder_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    intended_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('scheduled','claimed','cancelled','delivered')),
    created_at_ms INTEGER NOT NULL,
    claimed_at_ms INTEGER,
    actual_fire_at_ms INTEGER,
    cancelled_at_ms INTEGER,
    delivered_at_ms INTEGER,
    delivered_event_id TEXT UNIQUE,
    -- The Server sees only scheduled/cancelled/delivered.  These fields make
    -- the existing occurrence row its own durable outbox; no second queue or
    -- database is needed for remote reconciliation.
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    server_ack_revision INTEGER NOT NULL DEFAULT 0 CHECK (server_ack_revision >= 0),
    payload_format TEXT CHECK (payload_format IS NULL OR length(payload_format) BETWEEN 1 AND 64),
    opaque_payload BLOB CHECK (opaque_payload IS NULL OR length(opaque_payload) BETWEEN 1 AND 32768),
    sync_retry_after_ms INTEGER,
    sync_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (sync_retry_count >= 0 AND sync_retry_count <= 20),
    sync_permanent_revision INTEGER,
    sync_permanent_code TEXT CHECK (sync_permanent_code IS NULL OR length(sync_permanent_code) <= 128),
    delivery_claim_id TEXT,
    delivery_claim_acquired INTEGER NOT NULL DEFAULT 0,
    snapshot_conflict INTEGER NOT NULL DEFAULT 0
        CHECK (snapshot_conflict IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_reminder_occurrences_due
    ON reminder_occurrences (state, intended_at_ms, occurrence_id);
"""


class MessageStore(ReminderStoreMixin, InboxStoreMixin):
    """Own all durable message, Inbox, and reminder state for one Agent."""

    NOTICE_WINDOW_MS = 3_000

    def __init__(
        self,
        db_path: str | Path,
        *,
        now_ms: Any = _now_ms,
    ):
        self.db_path = Path(db_path)
        self._now_ms = now_ms
        self._db: Optional[aiosqlite.Connection] = None
        self._open_lock = asyncio.Lock()
        # aiosqlite multiplexes one connection. Keep every Inbox transaction
        # and lifecycle-sensitive read outside another coroutine's transaction.
        self._inbox_lock = asyncio.Lock()

    @staticmethod
    def for_agent(agent_id: str) -> MessageStore:
        return MessageStore(home_dir() / "agents" / agent_id / "messages.db")

    async def open(self) -> None:
        async with self._open_lock:
            if self._db is not None:
                return

            async with _schema_init_lock(self.db_path):
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(str(self.db_path), timeout=30.0)
                db.row_factory = aiosqlite.Row
                try:
                    await self._configure_connection(db)
                    # Phase 1: tables that do not depend on additive Inbox columns.
                    await self._execute_locked_script(db, _BASE_SCHEMA)

                    # Acquire the database write lock before inspecting columns.
                    # Separate stores may initialize the same file concurrently.
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        cur = await db.execute("PRAGMA table_info(messages)")
                        cols = {row[1] for row in await cur.fetchall()}
                        additions = {
                            "is_encrypted": "is_encrypted INTEGER NOT NULL DEFAULT 1",
                            "server_seq": "server_seq INTEGER",
                            "receipt_disposition": (
                                "receipt_disposition TEXT CHECK (receipt_disposition IS NULL OR "
                                "receipt_disposition IN ('eligible','terminal','foreign_dm_gated','local_runtime'))"
                            ),
                            "receipt_reason": "receipt_reason TEXT",
                            "processing_state": (
                                "processing_state TEXT CHECK (processing_state IS NULL OR "
                                "processing_state IN ('pending','in_turn','processed'))"
                            ),
                            "processing_turn_id": "processing_turn_id TEXT",
                            "model_visible_at": "model_visible_at INTEGER",
                            "processed_at": "processed_at INTEGER",
                            "local_ordinal": "local_ordinal INTEGER",
                            "after_server_seq": "after_server_seq INTEGER",
                        }
                        for name, declaration in additions.items():
                            if name not in cols:
                                await db.execute(
                                    f"ALTER TABLE messages ADD COLUMN {declaration}"
                                )
                        await db.commit()
                    except BaseException:
                        await db.rollback()
                        raise

                    # Phase 3: indexes and tables whose definitions use the new columns.
                    await self._execute_locked_script(db, _DEPENDENT_SCHEMA)
                    await self._migrate_reminder_sync_schema(db)
                    await self._migrate_nullable_provider_session(db)
                    await self._migrate_notice_provider_session(db)
                    self._db = db
                except BaseException:
                    await db.close()
                    raise

    @staticmethod
    async def _configure_connection(db: aiosqlite.Connection) -> None:
        # Setting WAL on a brand-new database can fail immediately when
        # another process is doing the same. Retry that one-time transition;
        # later schema work is serialized by BEGIN IMMEDIATE.
        await db.execute("PRAGMA busy_timeout=1000")
        deadline = time.monotonic() + 30.0
        delay = 0.01
        while True:
            try:
                async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite refused WAL journal mode")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.25)

        await db.execute("PRAGMA busy_timeout=30000")
        # A transport receipt is ACKed immediately after its commit. FULL is
        # required so a power loss cannot erase an already-ACKed delivery.
        await db.execute("PRAGMA synchronous=FULL")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA cache_size=-20000")
        await db.execute("PRAGMA mmap_size=268435456")

    @staticmethod
    async def _execute_locked_script(db: aiosqlite.Connection, script: str) -> None:
        try:
            await db.executescript(f"BEGIN IMMEDIATE;\n{script}\nCOMMIT;")
        except BaseException:
            await db.rollback()
            raise

    async def close(self) -> None:
        async with self._open_lock:
            if self._db:
                await self._db.close()
                self._db = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.open()
        assert self._db is not None
        return self._db

    async def store(self, payload: Any, *, received_at: int | None = None) -> None:
        async with self._inbox_lock:
            await self._store_unlocked(payload, received_at=received_at)

    async def _store_unlocked(
        self, payload: Any, *, received_at: int | None = None
    ) -> None:
        db = await self._ensure_db()
        if isinstance(payload, dict):
            envelope_id = payload.get("envelope_id", "")
            envelope_kind = payload.get("envelope_kind", "channel")
            sender_slug = payload.get("sender_slug", "")
            channel_id = payload.get("channel_id")
            space_id = payload.get("space_id")
            recipient_slug = payload.get("recipient_slug")
            content_type = payload.get("content_type", "text/plain")
            content = payload.get("content", "")
            sent_at = payload.get("sent_at", _now_ms())
            thread_root_id = payload.get("thread_root_id")
            reply_to_id = payload.get("reply_to_id")
            is_encrypted = payload.get("is_encrypted", True)
        else:
            envelope_id = payload.envelope_id
            envelope_kind = payload.envelope_kind
            sender_slug = payload.sender_slug
            channel_id = payload.channel_id
            space_id = payload.space_id
            recipient_slug = payload.recipient_slug
            content_type = payload.content_type
            content = payload.content
            sent_at = payload.sent_at
            thread_root_id = getattr(payload, "thread_root_id", None)
            reply_to_id = getattr(payload, "reply_to_id", None)
            is_encrypted = getattr(payload, "is_encrypted", True)

        content_str = _encode_content(content)
        if received_at is None:
            received_at = _now_ms()

        await db.execute(
            """INSERT OR IGNORE INTO messages
            (envelope_id, envelope_kind, sender_slug, channel_id, space_id,
             recipient_slug, content_type, content, sent_at, received_at,
             thread_root_id, reply_to_id, is_encrypted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                envelope_id,
                envelope_kind,
                sender_slug,
                channel_id,
                space_id,
                recipient_slug,
                content_type,
                content_str,
                sent_at,
                received_at,
                thread_root_id,
                reply_to_id,
                1 if is_encrypted else 0,
            ),
        )
        await db.commit()

    @staticmethod
    def _payload_values(payload: Any, received_at: int | None) -> tuple[Any, ...]:
        def value(name: str, default: Any = None) -> Any:
            if isinstance(payload, dict):
                return payload.get(name, default)
            return getattr(payload, name, default)

        envelope_id = value("envelope_id", "")
        content = value("content", "")
        content_str = _encode_content(content)
        return (
            envelope_id,
            value("envelope_kind", "channel"),
            value("sender_slug", ""),
            value("channel_id"),
            value("space_id"),
            value("recipient_slug"),
            value("content_type", "text/plain"),
            content_str,
            value("sent_at", _now_ms()),
            received_at if received_at is not None else _now_ms(),
            value("thread_root_id"),
            value("reply_to_id"),
            1 if value("is_encrypted", True) else 0,
        )

    @staticmethod
    def _receipt_ack(disposition: ReceiptDisposition) -> bool:
        return disposition in (
            ReceiptDisposition.ELIGIBLE,
            ReceiptDisposition.TERMINAL,
        )

    async def store_receipt(
        self,
        payload: Any,
        *,
        server_seq: int,
        disposition: ReceiptDisposition,
        reason: str,
        received_at: int | None = None,
    ) -> ReceiptResult:
        async with self._inbox_lock:
            return await self._store_receipt_unlocked(
                payload,
                server_seq=server_seq,
                disposition=disposition,
                reason=reason,
                received_at=received_at,
            )

    async def _store_receipt_unlocked(
        self,
        payload: Any,
        *,
        server_seq: int,
        disposition: ReceiptDisposition,
        reason: str,
        received_at: int | None = None,
    ) -> ReceiptResult:
        from .receipt_persistence import store_receipt_unlocked

        return await store_receipt_unlocked(
            self,
            payload,
            server_seq=server_seq,
            disposition=disposition,
            reason=reason,
            received_at=received_at,
        )

    async def store_local_receipt(
        self,
        payload: Any,
        *,
        disposition: ReceiptDisposition,
        reason: str,
        received_at: int | None = None,
    ) -> ReceiptResult:
        """Persist one gate verdict for a delivery that carries no ``server_seq``.

        The cloud-bridge ``Message`` frame has no delivery sequence, so the
        keyless transport cannot use ``store_receipt``. It still has to persist
        the *verdict*: a plain ``store()`` writes a NULL disposition, which
        makes a held foreign DM unpromotable and indistinguishable from an
        un-classified row. This is ``store_receipt``'s sequence-less sibling —
        same dispositions, same ack rule, local ordering instead of a server
        frontier.
        """
        async with self._inbox_lock:
            from .receipt_persistence import store_local_receipt_unlocked

            return await store_local_receipt_unlocked(
                self,
                payload,
                disposition=disposition,
                reason=reason,
                received_at=received_at,
            )

    async def promote_gated_receipt(
        self,
        envelope_id: str,
        server_seq: int | None,
        *,
        reason: str,
        approved_payload: Any | None = None,
    ) -> ReceiptResult:
        """Release one approval-gated receipt into the pending queue.

        ``server_seq`` is the sequence the gated row was written with, or
        ``None`` for the sequence-less keyless lane. Both lanes run the same
        state machine; only the terminal shape differs, because a row without
        a server sequence is only visible to ``get_pending`` as a local
        runtime event with an allocated ordinal. ``approved_payload`` replaces
        the gated body with the fully enriched model projection atomically.
        """
        replacement = None
        if approved_payload is not None:
            values = self._payload_values(approved_payload, None)
            if values[0] != envelope_id:
                raise ValueError("approved payload belongs to another envelope")
            replacement = (values[6], values[7])
        async with self._inbox_lock:
            return await self._promote_gated_receipt_unlocked(
                envelope_id,
                server_seq,
                reason=reason,
                replacement=replacement,
            )

    async def _promote_gated_receipt_unlocked(
        self,
        envelope_id: str,
        server_seq: int | None,
        *,
        reason: str,
        replacement: tuple[Any, Any] | None,
    ) -> ReceiptResult:
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT server_seq, receipt_disposition, processing_state, "
                "received_at, content_type, content "
                "FROM messages WHERE envelope_id = ?",
                (envelope_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if server_seq is None:
                return await self._promote_local_gated_row(
                    db,
                    row,
                    envelope_id=envelope_id,
                    reason=reason,
                    replacement=replacement,
                )
            if row is None or row["server_seq"] != server_seq:
                await db.rollback()
                return ReceiptResult(
                    ReceiptWriteStatus.CONFLICT,
                    ReceiptDisposition.FOREIGN_DM_GATED,
                    "gated receipt association mismatch",
                    False,
                )
            if (
                row["receipt_disposition"] == ReceiptDisposition.ELIGIBLE.value
                and row["processing_state"] == ProcessingState.PENDING.value
            ):
                await db.rollback()
                return ReceiptResult(
                    ReceiptWriteStatus.IDEMPOTENT,
                    ReceiptDisposition.ELIGIBLE,
                    reason,
                    True,
                )
            if (
                row["receipt_disposition"] != ReceiptDisposition.FOREIGN_DM_GATED.value
                or row["processing_state"] is not None
            ):
                await db.rollback()
                return ReceiptResult(
                    ReceiptWriteStatus.CONFLICT,
                    ReceiptDisposition.FOREIGN_DM_GATED,
                    "receipt is not approval-gated",
                    False,
                )
            content_type, content = replacement or (
                row["content_type"],
                row["content"],
            )
            await db.execute(
                """UPDATE messages
                   SET receipt_disposition = ?, receipt_reason = ?, processing_state = ?,
                       content_type = ?, content = ?
                   WHERE envelope_id = ? AND server_seq = ?
                     AND receipt_disposition = ? AND processing_state IS NULL""",
                (
                    ReceiptDisposition.ELIGIBLE.value,
                    reason,
                    ProcessingState.PENDING.value,
                    content_type,
                    content,
                    envelope_id,
                    server_seq,
                    ReceiptDisposition.FOREIGN_DM_GATED.value,
                ),
            )
            await self._refresh_notice_state(db)
            await db.commit()
            return ReceiptResult(
                ReceiptWriteStatus.COMMITTED,
                ReceiptDisposition.ELIGIBLE,
                reason,
                True,
            )
        except Exception:
            await db.rollback()
            raise

    async def _promote_local_gated_row(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row | None,
        *,
        envelope_id: str,
        reason: str,
        replacement: tuple[Any, Any] | None,
    ) -> ReceiptResult:
        """Release a gated row that was written without a server sequence.

        Its released shape is the keyless *eligible* shape — ``local_runtime``
        pending with an allocated ordinal — because that is the only pending
        shape ``_get_pending_unlocked`` recognises for a row with no
        ``server_seq``. Promoting it to ``eligible`` instead would leave the
        approved DM durably invisible, which is the failure this replaces.
        """
        if row is None or row["server_seq"] is not None:
            await db.rollback()
            return ReceiptResult(
                ReceiptWriteStatus.CONFLICT,
                ReceiptDisposition.FOREIGN_DM_GATED,
                "gated receipt association mismatch",
                False,
            )
        if (
            row["receipt_disposition"] == ReceiptDisposition.LOCAL_RUNTIME.value
            and row["processing_state"] == ProcessingState.PENDING.value
        ):
            await db.rollback()
            return ReceiptResult(
                ReceiptWriteStatus.IDEMPOTENT,
                ReceiptDisposition.LOCAL_RUNTIME,
                reason,
                True,
            )
        if (
            row["receipt_disposition"] != ReceiptDisposition.FOREIGN_DM_GATED.value
            or row["processing_state"] is not None
        ):
            await db.rollback()
            return ReceiptResult(
                ReceiptWriteStatus.CONFLICT,
                ReceiptDisposition.FOREIGN_DM_GATED,
                "receipt is not approval-gated",
                False,
            )
        ordinal, frontier = await self._allocate_local_position(db)
        content_type, content = replacement or (
            row["content_type"],
            row["content"],
        )
        await db.execute(
            """UPDATE messages
               SET receipt_disposition = ?, receipt_reason = ?,
                   processing_state = ?, local_ordinal = ?, after_server_seq = ?,
                   content_type = ?, content = ?
               WHERE envelope_id = ? AND server_seq IS NULL
                 AND receipt_disposition = ? AND processing_state IS NULL""",
            (
                ReceiptDisposition.LOCAL_RUNTIME.value,
                reason,
                ProcessingState.PENDING.value,
                ordinal,
                frontier,
                content_type,
                content,
                envelope_id,
                ReceiptDisposition.FOREIGN_DM_GATED.value,
            ),
        )
        await self._refresh_notice_state(db, activated_at=int(row["received_at"]))
        await db.commit()
        return ReceiptResult(
            ReceiptWriteStatus.COMMITTED,
            ReceiptDisposition.LOCAL_RUNTIME,
            reason,
            True,
        )

    async def store_local_event(
        self,
        payload: Any,
        *,
        reason: str,
        intro_channel_id: str | None = None,
        received_at: int | None = None,
    ) -> StoredMessage:
        async with self._inbox_lock:
            return await self._store_local_event_unlocked(
                payload,
                reason=reason,
                intro_channel_id=intro_channel_id,
                received_at=received_at,
            )

    async def _store_local_event_unlocked(
        self,
        payload: Any,
        *,
        reason: str,
        intro_channel_id: str | None = None,
        received_at: int | None = None,
    ) -> StoredMessage:
        values = self._payload_values(payload, received_at)
        envelope_id = values[0]
        if not isinstance(envelope_id, str) or not envelope_id:
            raise ValueError("payload must contain a non-empty envelope_id")
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            if intro_channel_id:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO channel_intro_prompted(channel_id, prompted_at) "
                    "VALUES (?, ?)",
                    (intro_channel_id, _now_ms()),
                )
                if cursor.rowcount == 0:
                    async with db.execute(
                        "SELECT receipt_disposition FROM messages WHERE envelope_id = ?",
                        (envelope_id,),
                    ) as existing_cursor:
                        existing = await existing_cursor.fetchone()
                    if (
                        existing is None
                        or existing["receipt_disposition"]
                        != ReceiptDisposition.LOCAL_RUNTIME.value
                    ):
                        raise LifecycleConflict(
                            "introduction marker already belongs to another local event"
                        )
                    await db.rollback()
                    message = await self._get_message_by_envelope_unlocked(
                        db, envelope_id
                    )
                    assert message is not None
                    return message

            await self._insert_local_event_in_transaction(
                db,
                values=values,
                reason=reason,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        message = await self._get_message_by_envelope_unlocked(db, envelope_id)
        assert message is not None
        return message

    async def _insert_local_event_in_transaction(
        self,
        db: aiosqlite.Connection,
        *,
        values: tuple[Any, ...],
        reason: str,
    ) -> None:
        """Insert one local pending event while a caller owns ``BEGIN IMMEDIATE``.

        Reminder delivery needs this exact insertion plus an occurrence state
        transition in one SQLite transaction.  Keeping allocation here avoids
        nesting ``store_local_event`` transactions while preserving the same
        local frontier, ordinal, and notice-generation semantics for every
        local event type.
        """
        ordinal, frontier = await self._allocate_local_position(db)
        await db.execute(
            """INSERT INTO messages
               (envelope_id, envelope_kind, sender_slug, channel_id, space_id,
                recipient_slug, content_type, content, sent_at, received_at,
                thread_root_id, reply_to_id, is_encrypted, receipt_disposition,
                receipt_reason, processing_state, local_ordinal, after_server_seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values
            + (
                ReceiptDisposition.LOCAL_RUNTIME.value,
                reason,
                ProcessingState.PENDING.value,
                ordinal,
                frontier,
            ),
        )
        await self._refresh_notice_state(db, activated_at=int(values[9]))

    async def _allocate_local_position(
        self, db: aiosqlite.Connection
    ) -> tuple[int, int | None]:
        """Claim the next local ordinal and durable Server frontier.

        Every row that has no ``server_seq`` but must still take a definite
        place in Inbox and prior-context ordering allocates through here, so
        local events, sequence-less gate verdicts, and released gated DMs all
        share one monotonic sequence. The caller owns ``BEGIN IMMEDIATE``.
        """
        async with db.execute(
            """SELECT high_water FROM server_sequence_high_water
               WHERE singleton = 1"""
        ) as cursor:
            frontier_row = await cursor.fetchone()
        if frontier_row is None:
            raise LifecycleConflict("server sequence high-water is unavailable")
        frontier_value = int(frontier_row[0])
        frontier = frontier_value if frontier_value > 0 else None
        async with db.execute(
            "SELECT next_ordinal FROM local_ordinal_allocator WHERE singleton = 1"
        ) as cursor:
            ordinal_row = await cursor.fetchone()
        ordinal = int(ordinal_row[0])
        await db.execute(
            "UPDATE local_ordinal_allocator SET next_ordinal = ? WHERE singleton = 1",
            (ordinal + 1,),
        )
        return ordinal, frontier

    @staticmethod
    async def _advance_server_sequence_high_water(
        db: aiosqlite.Connection, server_seq: int
    ) -> None:
        """Advance the durable receipt frontier inside the caller's transaction."""
        cursor = await db.execute(
            """UPDATE server_sequence_high_water
               SET high_water = MAX(high_water, ?)
               WHERE singleton = 1""",
            (server_seq,),
        )
        if cursor.rowcount != 1:
            raise LifecycleConflict("server sequence high-water is unavailable")

    async def has_known_channel_rows_above_baseline(
        self,
        space_id: str,
        channel_id: str,
    ) -> bool:
        """Whether a candidate advance has local channel evidence to respect."""
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                """SELECT 1 FROM messages
                   WHERE space_id = ? AND channel_id = ?
                     AND envelope_kind != 'dm' AND server_seq IS NOT NULL
                     AND server_seq > COALESCE((
                       SELECT context_baseline_seq FROM channel_context_state
                       WHERE space_id = ? AND channel_id = ?
                     ), -1)
                   LIMIT 1""",
                (space_id, channel_id, space_id, channel_id),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def channel_exists(self, channel_id: str) -> bool:
        """True iff the store has ever recorded a message in
        ``channel_id``. Used by the data service to return 404 when
        the caller asks for history on a channel the agent has never
        seen — distinguishes "unknown channel" from "known channel,
        empty window after filters" (200 + empty list).
        """
        if not channel_id:
            return False
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def has_message(self, envelope_id: str) -> bool:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE envelope_id = ?", (envelope_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def lookup_channel_space(self, channel_id: str) -> str | None:
        """Return the ``space_id`` known for ``channel_id``, or
        ``None`` when neither the explicit map nor any prior message
        gives one. Used by the MCP subprocess (which can't read the
        daemon's in-memory ``_channel_space`` map) as a cross-space
        fallback.

        Two-source lookup, in order:

        1. ``channel_space_map`` — explicit mappings recorded by
           out-of-band discovery (``_find_public_general_channel`` and
           friends). Lets send_message resolve a channel BEFORE the
           first inbound message lands on it — the case the intro
           nudge needs.
        2. ``messages`` — last ``space_id`` seen on an envelope in
           that channel. Steady-state fallback that doesn't need
           explicit bookkeeping; works automatically once any message
           arrives.
        """
        if not channel_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT space_id FROM channel_space_map WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and row[0]:
            return row[0]
        async with db.execute(
            "SELECT space_id FROM messages WHERE channel_id = ? "
            "AND space_id IS NOT NULL AND space_id != '' "
            "ORDER BY sent_at DESC LIMIT 1",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row[0] if row[0] else None

    async def mark_channel_space(self, channel_id: str, space_id: str) -> None:
        """Record an explicit channel→space mapping. Called by
        out-of-band channel-discovery paths (``_find_public_general_channel``)
        so ``lookup_channel_space`` can resolve the channel before
        the first inbound message lands."""
        if not channel_id or not space_id:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                """INSERT INTO channel_space_map (channel_id, space_id, learned_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel_id) DO UPDATE SET
                     space_id = excluded.space_id,
                     learned_at = excluded.learned_at""",
                (channel_id, space_id, _now_ms()),
            )
            await db.commit()

    async def unmark_channel_space(self, channel_id: str) -> None:
        """Drop a single channel→space mapping. Called by the per-
        channel eviction path (``_on_kicked_from_channel`` /
        ``_on_left_channel`` in puffo_core_client) so subsequent
        MCP-tool resolution doesn't keep routing into a channel
        we're no longer in. Idempotent — non-existent rows are
        silently skipped."""
        if not channel_id:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                "DELETE FROM channel_space_map WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    async def unmark_channel_space_for_space(self, space_id: str) -> None:
        """Per-space companion to ``unmark_channel_space`` — drops
        every channel→space mapping whose space matches. Called by
        the space-level eviction path (``_on_left_space`` /
        ``_on_kicked_from_space``) when the agent has been removed
        from a whole space, so MCP-tool resolution for any of that
        space's channels fast-fails locally instead of round-tripping
        the server for a guaranteed-403."""
        if not space_id:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                "DELETE FROM channel_space_map WHERE space_id = ?",
                (space_id,),
            )
            await db.commit()

    async def get_channel_history(
        self,
        channel_id: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[StoredMessage]:
        db = await self._ensure_db()
        if before is not None:
            sql = """SELECT * FROM messages
                     WHERE channel_id = ? AND sent_at < ?
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params: tuple = (channel_id, before, limit)
        else:
            sql = """SELECT * FROM messages
                     WHERE channel_id = ?
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params = (channel_id, limit)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def get_held_reconsideration_rows(
        self,
        *,
        space_id: str,
        channel_id: str,
        after_seq: int,
        through_seq: int,
        limit: int = 50,
    ) -> list[StoredMessage]:
        """Return the bounded locally-decrypted channel interval for a held send.

        The caller still proves the terminal envelope separately.  Keeping this
        query in the store makes it impossible for the server response to be
        mistaken for plaintext context.
        """
        db = await self._ensure_db()
        async with db.execute(
            """SELECT * FROM messages
               WHERE space_id = ? AND channel_id = ?
                 AND server_seq IS NOT NULL AND server_seq > ? AND server_seq <= ?
               ORDER BY server_seq ASC LIMIT ?""",
            (space_id, channel_id, after_seq, through_seq, max(1, min(limit, 200))),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(row) for row in rows]

    async def get_dm_history(
        self,
        peer_slug: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[StoredMessage]:
        db = await self._ensure_db()
        if before is not None:
            sql = f"""SELECT * FROM messages
                     WHERE envelope_kind = 'dm'
                       AND (sender_slug = ? OR recipient_slug = ?)
                       AND sent_at < ? AND {_NOT_GATED}
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params: tuple = (peer_slug, peer_slug, before, limit)
        else:
            sql = f"""SELECT * FROM messages
                     WHERE envelope_kind = 'dm'
                       AND (sender_slug = ? OR recipient_slug = ?)
                       AND {_NOT_GATED}
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params = (peer_slug, peer_slug, limit)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def get_message_by_envelope(
        self,
        envelope_id: str,
    ) -> Optional[StoredMessage]:
        """Single-row lookup by envelope_id. Returns ``None`` when
        the agent never saw the message. Lets this class act as an
        in-process drop-in for ``mcp.data_client.DataClient`` in hosts
        that skip the loopback HTTP round-trip.
        """
        if not envelope_id:
            return None
        async with self._inbox_lock:
            db = await self._ensure_db()
            return await self._get_message_by_envelope_unlocked(db, envelope_id)

    async def get_visible_message_by_envelope(
        self,
        envelope_id: str,
    ) -> Optional[StoredMessage]:
        """Model-visible sibling of ``get_message_by_envelope``.

        Withholds a ``foreign_dm_gated`` row exactly as the DM and thread
        reads do, so a stranger's held DM cannot reach the model through a
        single-envelope lookup while the operator has yet to approve it.
        Runtime-internal callers keep the unfiltered read — redelivery
        dedupe, gate promotion, and held/admission bookkeeping all have to
        see the held row.
        """
        if not envelope_id:
            return None
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                f"SELECT * FROM messages WHERE envelope_id = ? AND {_NOT_GATED}",
                (envelope_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return None if row is None else self._row_to_msg(row)

    async def _get_message_by_envelope_unlocked(
        self, db: aiosqlite.Connection, envelope_id: str
    ) -> Optional[StoredMessage]:
        async with db.execute(
            "SELECT * FROM messages WHERE envelope_id = ?",
            (envelope_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_msg(row)

    async def get_thread_batch(
        self,
        root_id: str,
        since_sent_at: int,
    ) -> list[StoredMessage]:
        """Root message + every reply in its thread with
        ``sent_at > since_sent_at``, ordered ascending. The root row
        itself has ``thread_root_id IS NULL`` and matches via the
        ``envelope_id = root_id`` arm of the OR.
        """
        if not root_id:
            return []
        db = await self._ensure_db()
        async with db.execute(
            f"""SELECT * FROM messages
               WHERE (envelope_id = ? OR thread_root_id = ?)
                 AND sent_at > ? AND {_NOT_GATED}
               ORDER BY sent_at ASC, envelope_id ASC""",
            (root_id, root_id, since_sent_at),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def _resolve_since_sent_at(
        self, since_envelope_id: str | None
    ) -> tuple[int, str] | None:
        """Look up the ``sent_at`` of a reference envelope. Used by
        ``get_channel_roots`` / ``get_thread_messages`` to translate
        a ``since=<envelope_id>`` filter into an exclusive sent_at
        lower bound. Returns ``None`` when the envelope isn't in the
        store (caller treats that as "no since filter")."""
        if not since_envelope_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT sent_at, envelope_id FROM messages WHERE envelope_id = ?",
            (since_envelope_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row[0]), str(row[1])) if row else None

    async def get_channel_roots(
        self,
        channel_id: str,
        limit: int = 20,
        since_envelope_id: str | None = None,
        before_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[ChannelRoot]:
        """Recent root posts in ``channel_id`` (``thread_root_id``
        IS NULL) with the count of replies that point at each.

        Replies in any thread are excluded from the result — the
        agent gets one bullet per conversation head and can drill
        into the threads it actually cares about via
        ``get_thread_messages``. Filtering options:

        - ``since_envelope_id`` — return roots whose ``sent_at`` is
          strictly greater than that envelope's. Convenient when
          the agent already knows the latest root it processed.
        - ``before_envelope_id`` — return roots strictly before that
          composite ``(sent_at, envelope_id)`` cursor. This permits stable
          reverse pagination when several envelopes share a timestamp.
        - ``after_ts`` (ms-epoch) — exclusive lower bound on
          ``sent_at``. Combined with ``since`` we take the larger.
        - ``before_ts`` (ms-epoch) — exclusive upper bound on
          ``sent_at``.

        Returned newest-first up to ``limit``. Raises
        ``DataNotFound`` if the channel has never had any message
        stored — so the MCP layer can distinguish "unknown channel"
        from "known but empty window".
        """
        if not await self.channel_exists(channel_id):
            raise DataNotFound(f"channel not found: {channel_id}")
        db = await self._ensure_db()
        since_resolved = await self._resolve_since_sent_at(since_envelope_id)
        before_resolved = await self._resolve_since_sent_at(before_envelope_id)

        # ``reply_count`` is a correlated subquery on the same
        # ``messages`` table; the WAL writer is the only producer, so
        # the count is point-in-time consistent.
        clauses = ["m.channel_id = ?", "m.thread_root_id IS NULL"]
        params: list = [channel_id]
        if since_resolved is not None:
            clauses.append("(m.sent_at > ? OR (m.sent_at = ? AND m.envelope_id > ?))")
            params.extend([since_resolved[0], since_resolved[0], since_resolved[1]])
        if before_resolved is not None:
            clauses.append("(m.sent_at < ? OR (m.sent_at = ? AND m.envelope_id < ?))")
            params.extend([before_resolved[0], before_resolved[0], before_resolved[1]])
        if after_ts is not None:
            clauses.append("m.sent_at > ?")
            params.append(int(after_ts))
        if before_ts is not None:
            clauses.append("m.sent_at < ?")
            params.append(int(before_ts))
        where = " AND ".join(clauses)
        ascending_cursor = since_resolved is not None and before_resolved is None
        order = (
            "m.sent_at ASC, m.envelope_id ASC"
            if ascending_cursor
            else "m.sent_at DESC, m.envelope_id DESC"
        )
        sql = (
            "SELECT m.*, "
            "(SELECT COUNT(*) FROM messages r "
            " WHERE r.thread_root_id = m.envelope_id) AS reply_count "
            f"FROM messages m WHERE {where} ORDER BY {order} LIMIT ?"
        )
        params.append(max(1, min(int(limit), 200)))

        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        # Cursor pages already select oldest-first; uncursorred history is
        # selected newest-first then reversed for the public oldest-first view.
        return [
            ChannelRoot(
                message=self._row_to_msg(r),
                reply_count=int(r["reply_count"]),
            )
            for r in (rows if ascending_cursor else reversed(rows))
        ]

    async def get_thread_messages(
        self,
        root_id: str,
        limit: int = 50,
        since_envelope_id: str | None = None,
        before_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[StoredMessage]:
        """Messages belonging to a thread (the root itself plus
        every reply pointing at it), filtered the same way as
        ``get_channel_roots``. Newest-first selection, then
        reversed to oldest-first in the returned list — matches
        ``get_channel_history``'s shape so the MCP tool can format
        either the same way. Raises ``DataNotFound`` when no
        message with that envelope_id has been stored — same
        rationale as ``get_channel_roots``.
        """
        if not root_id:
            raise DataNotFound("thread root not found: (empty)")
        if not await self.has_message(root_id):
            raise DataNotFound(f"thread root not found: {root_id}")
        db = await self._ensure_db()
        since_resolved = await self._resolve_since_sent_at(since_envelope_id)
        before_resolved = await self._resolve_since_sent_at(before_envelope_id)

        clauses = ["(envelope_id = ? OR thread_root_id = ?)", _NOT_GATED]
        params: list = [root_id, root_id]
        if since_resolved is not None:
            clauses.append("(sent_at > ? OR (sent_at = ? AND envelope_id > ?))")
            params.extend([since_resolved[0], since_resolved[0], since_resolved[1]])
        if before_resolved is not None:
            clauses.append("(sent_at < ? OR (sent_at = ? AND envelope_id < ?))")
            params.extend([before_resolved[0], before_resolved[0], before_resolved[1]])
        if after_ts is not None:
            clauses.append("sent_at > ?")
            params.append(int(after_ts))
        if before_ts is not None:
            clauses.append("sent_at < ?")
            params.append(int(before_ts))
        where = " AND ".join(clauses)
        ascending_cursor = since_resolved is not None and before_resolved is None
        order = (
            "sent_at ASC, envelope_id ASC"
            if ascending_cursor
            else "sent_at DESC, envelope_id DESC"
        )
        sql = f"SELECT * FROM messages WHERE {where} ORDER BY {order} LIMIT ?"
        params.append(max(1, min(int(limit), 200)))

        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [
            self._row_to_msg(r) for r in (rows if ascending_cursor else reversed(rows))
        ]

    async def has_channel_intro_been_prompted(self, channel_id: str) -> bool:
        """True iff the agent already had a self-introduction prompted
        for ``channel_id``. Used by ``_accept_invite`` to gate the
        synthetic system-message enqueue so a restart-time replay of
        the same pending invite doesn't fire a second intro."""
        if not channel_id:
            return False
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM channel_intro_prompted WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_dm_notice(self, sender_slug: str) -> int | None:
        """Epoch-ms of the last FYI notice for ``sender_slug``, or None."""
        if not sender_slug:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT last_notified_at FROM dm_notices WHERE sender_slug = ?",
            (sender_slug,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def has_dm_from(self, sender_slug: str) -> bool:
        """Any stored inbound DM from ``sender_slug``."""
        if not sender_slug:
            return False
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE envelope_kind = 'dm' "
            "AND sender_slug = ? LIMIT 1",
            (sender_slug,),
        ) as cur:
            return await cur.fetchone() is not None

    async def set_dm_notice(self, sender_slug: str, notified_at: int) -> None:
        if not sender_slug:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                """INSERT INTO dm_notices (sender_slug, last_notified_at)
                   VALUES (?, ?)
                   ON CONFLICT(sender_slug) DO UPDATE SET
                     last_notified_at = excluded.last_notified_at""",
                (sender_slug, notified_at),
            )
            await db.commit()

    async def mark_channel_intro_prompted(self, channel_id: str) -> None:
        """Record that an intro nudge has been enqueued for
        ``channel_id``. Idempotent."""
        if not channel_id:
            return
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                """INSERT INTO channel_intro_prompted (channel_id, prompted_at)
                   VALUES (?, ?)
                   ON CONFLICT(channel_id) DO NOTHING""",
                (channel_id, _now_ms()),
            )
            await db.commit()

    async def tombstone_gated_dms_from(self, sender_slug: str) -> int:
        """Discard the plaintext of every DM still gated from ``sender_slug``.

        Called when the operator denies a foreign-DM approval. ``cleanup()``
        deliberately never expires a gated row — right for a hold that is
        still open, wrong once the answer is "no", which would otherwise
        retain the refused body indefinitely. The row itself survives as a
        terminal tombstone so redelivery dedupe and envelope accounting keep
        working; only the body the operator refused is gone.
        """
        if not sender_slug:
            return 0
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                """UPDATE messages
                   SET content = ?, content_type = 'text/plain',
                       receipt_disposition = ?, receipt_reason = ?
                   WHERE envelope_kind = 'dm' AND sender_slug = ?
                     AND receipt_disposition = ?""",
                (
                    _encode_content(DENIED_DM_PLACEHOLDER),
                    ReceiptDisposition.TERMINAL.value,
                    "foreign dm denied by operator",
                    sender_slug,
                    ReceiptDisposition.FOREIGN_DM_GATED.value,
                ),
            ) as cursor:
                count = cursor.rowcount
            await db.commit()
            return count

    async def cleanup(self, retention_days: int = 90) -> int:
        async with self._inbox_lock:
            db = await self._ensure_db()
            cutoff = _now_ms() - retention_days * 86_400_000
            async with db.execute(
                """DELETE FROM messages
                   WHERE received_at < ?
                     AND (processing_state IS NULL OR processing_state = 'processed')
                     AND (
                       receipt_disposition IS NULL
                       OR receipt_disposition != 'foreign_dm_gated'
                     )""",
                (cutoff,),
            ) as cursor:
                count = cursor.rowcount
            await db.commit()
            return count

    def _row_to_msg(self, row: aiosqlite.Row) -> StoredMessage:
        content = _decode_content(
            row["content"],
            content_type=str(row["content_type"] or ""),
            receipt_disposition=row["receipt_disposition"],
        )

        return StoredMessage(
            envelope_id=row["envelope_id"],
            envelope_kind=row["envelope_kind"],
            sender_slug=row["sender_slug"],
            channel_id=row["channel_id"],
            space_id=row["space_id"],
            recipient_slug=row["recipient_slug"],
            content_type=row["content_type"],
            content=content,
            sent_at=row["sent_at"],
            received_at=row["received_at"],
            thread_root_id=row["thread_root_id"],
            reply_to_id=row["reply_to_id"],
            is_encrypted=bool(row["is_encrypted"]),
            server_seq=row["server_seq"],
            receipt_disposition=(
                ReceiptDisposition(row["receipt_disposition"])
                if row["receipt_disposition"] is not None
                else None
            ),
            receipt_reason=row["receipt_reason"],
            processing_state=(
                ProcessingState(row["processing_state"])
                if row["processing_state"] is not None
                else None
            ),
            processing_turn_id=row["processing_turn_id"],
            model_visible_at=row["model_visible_at"],
            processed_at=row["processed_at"],
            local_ordinal=row["local_ordinal"],
            after_server_seq=row["after_server_seq"],
        )
