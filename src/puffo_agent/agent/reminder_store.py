"""Durable one-shot reminder persistence and reconciliation."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from .message_store_models import (
    _REMINDER_LOCAL_DELIVERY_SQL,
    _reminder_delivery_custody_sql,
    MAX_REMINDER_LIST_LIMIT,
    MAX_REMINDER_ENVELOPE_BYTES,
    REMINDER_STATES,
    LifecycleConflict,
    ReceiptDisposition,
    ReminderMaterializationResult,
    ReminderOccurrence,
    ReminderSyncRecord,
    parse_reminder_target,
    reminder_plaintext_envelope_size,
    reminder_time_to_rfc3339,
)


class ReminderStoreMixin:
    """Implementation trait for the single ``MessageStore`` state owner.

    It shares ``MessageStore`` persistence and locking and must not be used as
    an independently constructed store.
    """

    async def _migrate_reminder_sync_schema(self, db: aiosqlite.Connection) -> None:
        """Add v1 remote-sync state to the existing occurrence table.

        The first reminder slice already made ``reminder_occurrences`` the
        durable source of truth.  This migration deliberately keeps that
        table and adds its embedded outbox facts in one locked transaction.
        Old terminal rows predate a remotely visible scheduled revision, so
        their local terminal transition is represented as revision two.
        """
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("PRAGMA table_info(reminder_occurrences)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            additions = {
                # Nullable only for the duration of the backfill.  SQLite
                # cannot add a NOT NULL/CHECK column without rebuilding an
                # existing table, which would violate the additive contract.
                "revision": "revision INTEGER",
                "server_ack_revision": (
                    "server_ack_revision INTEGER NOT NULL DEFAULT 0"
                ),
                "payload_format": "payload_format TEXT",
                "opaque_payload": "opaque_payload BLOB",
                "sync_retry_after_ms": "sync_retry_after_ms INTEGER",
                "sync_retry_count": ("sync_retry_count INTEGER NOT NULL DEFAULT 0"),
                "sync_permanent_revision": "sync_permanent_revision INTEGER",
                "sync_permanent_code": "sync_permanent_code TEXT",
                "delivery_claim_id": "delivery_claim_id TEXT",
                "delivery_claim_acquired": (
                    "delivery_claim_acquired INTEGER NOT NULL DEFAULT 0"
                ),
                "delivery_claim_expires_at_ms": "delivery_claim_expires_at_ms INTEGER",
                "snapshot_conflict": (
                    "snapshot_conflict INTEGER NOT NULL DEFAULT 0"
                ),
            }
            added_revision = "revision" not in columns
            for name, declaration in additions.items():
                if name not in columns:
                    await db.execute(
                        f"ALTER TABLE reminder_occurrences ADD COLUMN {declaration}"
                    )
            if added_revision:
                await db.execute(
                    """UPDATE reminder_occurrences
                       SET revision = CASE
                           WHEN state IN ('scheduled', 'claimed') THEN 1
                           ELSE 2
                       END"""
                )
            # A defensive repair for partially created development databases;
            # no normal v1 row may retain a null/non-positive revision.
            await db.execute(
                """UPDATE reminder_occurrences
                   SET revision = CASE
                       WHEN state IN ('scheduled', 'claimed') THEN 1
                       ELSE 2
                   END
                   WHERE revision IS NULL OR revision <= 0"""
            )
            await db.execute(
                """UPDATE reminder_occurrences
                   SET snapshot_conflict = 0
                   WHERE snapshot_conflict IS NULL
                      OR snapshot_conflict NOT IN (0, 1)"""
            )
            await db.execute(
                """UPDATE reminder_occurrences
                   SET server_ack_revision = 0
                   WHERE server_ack_revision IS NULL OR server_ack_revision < 0"""
            )
            await db.execute(
                """UPDATE reminder_occurrences
                   SET delivery_claim_acquired = 0
                   WHERE delivery_claim_acquired IS NULL
                      OR delivery_claim_acquired NOT IN (0, 1)
                      OR delivery_claim_expires_at_ms IS NULL"""
            )
            await db.execute(
                """CREATE INDEX IF NOT EXISTS idx_reminder_occurrences_sync_pending
                   ON reminder_occurrences
                   (server_ack_revision, revision, sync_retry_after_ms, occurrence_id)"""
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    @staticmethod
    def _reminder_from_row(row: aiosqlite.Row) -> ReminderOccurrence:
        return ReminderOccurrence(
            reminder_id=str(row["reminder_id"]),
            occurrence_id=str(row["occurrence_id"]),
            target=str(row["target"]),
            content=str(row["content"]),
            intended_at_ms=int(row["intended_at_ms"]),
            state=str(row["state"]),
            created_at_ms=int(row["created_at_ms"]),
            claimed_at_ms=(
                int(row["claimed_at_ms"]) if row["claimed_at_ms"] is not None else None
            ),
            actual_fire_at_ms=(
                int(row["actual_fire_at_ms"])
                if row["actual_fire_at_ms"] is not None
                else None
            ),
            cancelled_at_ms=(
                int(row["cancelled_at_ms"])
                if row["cancelled_at_ms"] is not None
                else None
            ),
            delivered_at_ms=(
                int(row["delivered_at_ms"])
                if row["delivered_at_ms"] is not None
                else None
            ),
            delivered_event_id=(
                str(row["delivered_event_id"])
                if row["delivered_event_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _reminder_sync_record_from_row(row: aiosqlite.Row) -> ReminderSyncRecord:
        """Project a schema-upgraded occurrence for the private sync worker."""

        def optional_int(name: str) -> int | None:
            value = row[name]
            return int(value) if value is not None else None

        payload = row["opaque_payload"]
        return ReminderSyncRecord(
            reminder_id=str(row["reminder_id"]),
            occurrence_id=str(row["occurrence_id"]),
            target=str(row["target"]),
            content=str(row["content"]),
            intended_at_ms=int(row["intended_at_ms"]),
            state=str(row["state"]),
            created_at_ms=int(row["created_at_ms"]),
            claimed_at_ms=optional_int("claimed_at_ms"),
            actual_fire_at_ms=optional_int("actual_fire_at_ms"),
            cancelled_at_ms=optional_int("cancelled_at_ms"),
            delivered_at_ms=optional_int("delivered_at_ms"),
            delivered_event_id=(
                str(row["delivered_event_id"])
                if row["delivered_event_id"] is not None
                else None
            ),
            revision=int(row["revision"]),
            server_ack_revision=int(row["server_ack_revision"]),
            payload_format=(
                str(row["payload_format"])
                if row["payload_format"] is not None
                else None
            ),
            opaque_payload=bytes(payload) if payload is not None else None,
            sync_retry_after_ms=optional_int("sync_retry_after_ms"),
            sync_retry_count=int(row["sync_retry_count"]),
            sync_permanent_revision=optional_int("sync_permanent_revision"),
            sync_permanent_code=(
                str(row["sync_permanent_code"])
                if row["sync_permanent_code"] is not None
                else None
            ),
            delivery_claim_id=(
                str(row["delivery_claim_id"])
                if row["delivery_claim_id"] is not None
                else None
            ),
            delivery_claim_acquired=bool(row["delivery_claim_acquired"]),
            delivery_claim_expires_at_ms=optional_int(
                "delivery_claim_expires_at_ms"
            ),
            snapshot_conflict=bool(row["snapshot_conflict"]),
        )

    @staticmethod
    def _valid_reminder_time(value: int) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _validate_remote_scheduled_reminder(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
        opaque_payload: bytes,
    ) -> None:
        if (
            not all(
                isinstance(value, str) and value
                for value in (
                    reminder_id,
                    occurrence_id,
                    target,
                    content,
                    payload_format,
                )
            )
            or reminder_id == occurrence_id
            or not self._valid_reminder_time(intended_at_ms)
            or not self._valid_reminder_time(lifecycle_at_ms)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
            or len(payload_format) > 64
            or not payload_format.isascii()
            or any(char.isspace() for char in payload_format)
            or not isinstance(opaque_payload, bytes)
            or not 1 <= len(opaque_payload) <= 32_768
        ):
            raise ValueError("invalid remote reminder metadata")
        parse_reminder_target(target)

    def _validate_remote_terminal_reminder(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        intended_at_ms: int,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
    ) -> None:
        if (
            not all(
                isinstance(value, str) and value
                for value in (reminder_id, occurrence_id, lifecycle, payload_format)
            )
            or reminder_id == occurrence_id
            or lifecycle not in {"cancelled", "delivered"}
            or not self._valid_reminder_time(intended_at_ms)
            or not self._valid_reminder_time(lifecycle_at_ms)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
            or len(payload_format) > 64
            or not payload_format.isascii()
            or any(char.isspace() for char in payload_format)
        ):
            raise ValueError("invalid remote reminder tombstone")

    @staticmethod
    def _terminal_values(
        record: ReminderSyncRecord,
        lifecycle: str,
        lifecycle_at_ms: int,
    ) -> tuple[str, int | None, int | None, int | None, str | None]:
        next_state = "delivered" if record.state == "delivered" else lifecycle
        if next_state == "delivered":
            return (
                next_state,
                record.actual_fire_at_ms
                if record.state == "delivered"
                else lifecycle_at_ms,
                None,
                record.delivered_at_ms
                if record.state == "delivered"
                else lifecycle_at_ms,
                record.delivered_event_id if record.state == "delivered" else None,
            )
        return next_state, None, lifecycle_at_ms, None, None

    @classmethod
    def _merged_remote_terminal_values(
        cls,
        record: ReminderSyncRecord,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
    ) -> tuple[
        str,
        int | None,
        int | None,
        int | None,
        str | None,
        int,
        int,
    ]:
        values = cls._terminal_values(record, lifecycle, lifecycle_at_ms)
        next_state = values[0]
        next_revision = max(record.revision, revision)
        # The remote terminal revision is authoritative for future sync even
        # when a locally committed delivery fact must remain visible.
        next_ack_revision = max(record.server_ack_revision, revision)
        return (*values, next_revision, next_ack_revision)

    async def create_reminder(
        self,
        *,
        content: str,
        target: str,
        intended_at_ms: int,
        reminder_id: str | None = None,
        occurrence_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> ReminderOccurrence:
        """Durably create one immutable, single-fire reminder intent."""
        if not isinstance(content, str) or not content:
            raise ValueError("reminder content must be a non-empty string")
        parse_reminder_target(target)
        if (
            reminder_plaintext_envelope_size(target, content)
            > MAX_REMINDER_ENVELOPE_BYTES
        ):
            raise ValueError("reminder content exceeds the remote envelope limit")
        if not self._valid_reminder_time(intended_at_ms):
            raise ValueError("intended_at_ms must be an integer epoch milliseconds")
        if created_at_ms is None:
            created_at_ms = int(self._now_ms())
        if not self._valid_reminder_time(created_at_ms):
            raise ValueError("created_at_ms must be an integer epoch milliseconds")
        reminder_id = reminder_id or f"reminder-{uuid.uuid4()}"
        occurrence_id = occurrence_id or f"occurrence-{uuid.uuid4()}"
        if (
            not isinstance(reminder_id, str)
            or not reminder_id
            or not isinstance(occurrence_id, str)
            or not occurrence_id
            or reminder_id == occurrence_id
        ):
            raise ValueError("reminder and occurrence identities must be distinct")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """INSERT INTO reminder_occurrences
                       (reminder_id, occurrence_id, target, content, intended_at_ms,
                        state, created_at_ms, revision, server_ack_revision)
                       VALUES (?, ?, ?, ?, ?, 'scheduled', ?, 1, 0)""",
                    (
                        reminder_id,
                        occurrence_id,
                        target,
                        content,
                        intended_at_ms,
                        created_at_ms,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            reminder = await self._get_reminder_unlocked(db, reminder_id)
            assert reminder is not None
            return reminder

    async def _get_reminder_unlocked(
        self,
        db: aiosqlite.Connection,
        reminder_id: str,
    ) -> ReminderOccurrence | None:
        async with db.execute(
            "SELECT * FROM reminder_occurrences WHERE reminder_id = ?",
            (reminder_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._reminder_from_row(row) if row is not None else None

    async def get_reminder(self, reminder_id: str) -> ReminderOccurrence | None:
        if not isinstance(reminder_id, str) or not reminder_id:
            raise ValueError("reminder_id must be a non-empty string")
        async with self._inbox_lock:
            return await self._get_reminder_unlocked(
                await self._ensure_db(),
                reminder_id,
            )

    async def _get_reminder_sync_by_occurrence_unlocked(
        self,
        db: aiosqlite.Connection,
        occurrence_id: str,
    ) -> ReminderSyncRecord | None:
        async with db.execute(
            "SELECT * FROM reminder_occurrences WHERE occurrence_id = ?",
            (occurrence_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._reminder_sync_record_from_row(row) if row is not None else None

    async def get_reminder_sync_record(
        self,
        occurrence_id: str,
    ) -> ReminderSyncRecord | None:
        """Return a private worker-only sync view by immutable occurrence ID."""
        if not isinstance(occurrence_id, str) or not occurrence_id:
            raise ValueError("occurrence_id must be a non-empty string")
        async with self._inbox_lock:
            return await self._get_reminder_sync_by_occurrence_unlocked(
                await self._ensure_db(),
                occurrence_id,
            )

    async def get_reminder_sync_record_by_reminder_id(
        self,
        reminder_id: str,
    ) -> ReminderSyncRecord | None:
        """Return the worker-only sync view addressed by the MCP identity."""
        if not isinstance(reminder_id, str) or not reminder_id:
            raise ValueError("reminder_id must be a non-empty string")
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT * FROM reminder_occurrences WHERE reminder_id = ?",
                (reminder_id,),
            ) as cursor:
                row = await cursor.fetchone()
            return (
                self._reminder_sync_record_from_row(row)
                if row is not None
                else None
            )

    async def persist_reminder_envelope(
        self,
        *,
        occurrence_id: str,
        payload_format: str,
        opaque_payload: bytes,
    ) -> ReminderSyncRecord:
        """Persist one immutable opaque envelope, never replacing it.

        The sync worker may race a local terminal transition.  The terminal
        row still needs the original envelope for the Server's immutable-byte
        check, so this operation is allowed for every local lifecycle state.
        """
        if (
            not isinstance(occurrence_id, str)
            or not occurrence_id
            or not isinstance(payload_format, str)
            or not payload_format
            or len(payload_format) > 64
            or not payload_format.isascii()
            or any(char.isspace() for char in payload_format)
            or not isinstance(opaque_payload, bytes)
            or not 1 <= len(opaque_payload) <= 32_768
        ):
            raise ValueError("invalid reminder envelope metadata")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db,
                    occurrence_id,
                )
                if record is None:
                    raise ValueError("unknown reminder occurrence")
                if record.opaque_payload is not None:
                    if (
                        record.payload_format != payload_format
                        or record.opaque_payload != opaque_payload
                    ):
                        raise LifecycleConflict("reminder envelope is immutable")
                    await db.rollback()
                    return record
                await db.execute(
                    """UPDATE reminder_occurrences
                       SET payload_format = ?, opaque_payload = ?
                       WHERE occurrence_id = ? AND opaque_payload IS NULL""",
                    (payload_format, opaque_payload, occurrence_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            updated = await self._get_reminder_sync_by_occurrence_unlocked(
                db,
                occurrence_id,
            )
            assert updated is not None
            return updated

    async def pending_reminder_sync_records(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 100,
        force: bool = False,
        retry_permanent: bool = False,
    ) -> tuple[ReminderSyncRecord, ...]:
        """Find current unacknowledged revisions due for one bounded sync pass.

        ``retry_permanent`` is reserved for an explicit completed snapshot
        reconciliation.  Normal cadence never bypasses a permanent failure,
        so a malformed/conflicting Server response cannot create a tight loop.
        """
        now = int(self._now_ms()) if now_ms is None else now_ms
        if not self._valid_reminder_time(now):
            raise ValueError("now_ms must be an integer epoch milliseconds")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be between 1 and 200")
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                """SELECT * FROM reminder_occurrences
                   WHERE server_ack_revision < revision
                     AND snapshot_conflict = 0
                     AND (? = 1
                          OR sync_permanent_revision IS NULL
                          OR sync_permanent_revision != revision)
                     AND (? = 1 OR sync_retry_after_ms IS NULL
                          OR sync_retry_after_ms <= ?)
                   ORDER BY intended_at_ms, occurrence_id
                   LIMIT ?""",
                (1 if retry_permanent else 0, 1 if force else 0, now, limit),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(self._reminder_sync_record_from_row(row) for row in rows)

    async def acknowledge_reminder_sync_revision(
        self,
        *,
        occurrence_id: str,
        revision: int,
    ) -> bool:
        """Compare-and-set a Server acknowledgment without clearing newer work."""
        if not isinstance(occurrence_id, str) or not occurrence_id or revision <= 0:
            raise ValueError("invalid reminder acknowledgment")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE reminder_occurrences
                       SET server_ack_revision = ?,
                           sync_retry_after_ms = NULL,
                           sync_retry_count = 0,
                           sync_permanent_revision = NULL,
                           sync_permanent_code = NULL
                       WHERE occurrence_id = ? AND revision = ?
                         AND server_ack_revision < ?""",
                    (revision, occurrence_id, revision, revision),
                )
                changed = cursor.rowcount == 1
                await db.commit()
                return changed
            except BaseException:
                await db.rollback()
                raise

    async def schedule_reminder_sync_retry(
        self,
        *,
        occurrence_id: str,
        revision: int,
        retry_after_ms: int,
    ) -> bool:
        """Durably back off only the same current revision."""
        if (
            not isinstance(occurrence_id, str)
            or not occurrence_id
            or revision <= 0
            or not self._valid_reminder_time(retry_after_ms)
        ):
            raise ValueError("invalid reminder retry state")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE reminder_occurrences
                       SET sync_retry_after_ms = ?,
                           sync_retry_count = MIN(sync_retry_count + 1, 20),
                           sync_permanent_revision = NULL,
                           sync_permanent_code = NULL
                       WHERE occurrence_id = ? AND revision = ?""",
                    (retry_after_ms, occurrence_id, revision),
                )
                changed = cursor.rowcount == 1
                await db.commit()
                return changed
            except BaseException:
                await db.rollback()
                raise

    async def record_reminder_sync_permanent_failure(
        self,
        *,
        occurrence_id: str,
        revision: int,
        code: str,
    ) -> bool:
        """Suppress a malformed/conflicting revision without retaining payload text."""
        safe_code = (
            code
            if isinstance(code, str)
            and 1 <= len(code) <= 64
            and all(
                char.isascii() and (char.islower() or char.isdigit() or char in "_-")
                for char in code
            )
            else "contract_error"
        )
        if not isinstance(occurrence_id, str) or not occurrence_id or revision <= 0:
            raise ValueError("invalid reminder permanent failure")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE reminder_occurrences
                       SET sync_retry_after_ms = NULL,
                           sync_permanent_revision = ?,
                           sync_permanent_code = ?
                       WHERE occurrence_id = ? AND revision = ?""",
                    (revision, safe_code, occurrence_id, revision),
                )
                changed = cursor.rowcount == 1
                await db.commit()
                return changed
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    def _scheduled_snapshot_matches(
        record: ReminderSyncRecord,
        *,
        reminder_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        payload_format: str,
        opaque_payload: bytes,
    ) -> bool:
        return (
            record.reminder_id == reminder_id
            and record.target == target
            and record.content == content
            and record.intended_at_ms == intended_at_ms
            and record.payload_format == payload_format
            and record.opaque_payload == opaque_payload
        )

    @staticmethod
    def _terminal_materialization_changes(
        record: ReminderSyncRecord,
        values: tuple[str, int | None, int | None, int | None, str | None, int, int],
    ) -> bool:
        (
            next_state,
            actual_fire_at_ms,
            cancelled_at_ms,
            delivered_at_ms,
            delivered_event_id,
            next_revision,
            next_ack_revision,
        ) = values
        return (
            record.state != next_state
            or record.revision != next_revision
            or record.server_ack_revision < next_ack_revision
            or record.payload_format is None
            or record.actual_fire_at_ms != actual_fire_at_ms
            or record.cancelled_at_ms != cancelled_at_ms
            or record.delivered_at_ms != delivered_at_ms
            or record.delivered_event_id != delivered_event_id
            or record.claimed_at_ms is not None
            or record.sync_retry_after_ms is not None
            or record.sync_retry_count != 0
            or record.sync_permanent_revision is not None
            or record.sync_permanent_code is not None
            or record.delivery_claim_id is not None
            or record.delivery_claim_acquired
            or record.delivery_claim_expires_at_ms is not None
            or record.snapshot_conflict
        )

    @staticmethod
    async def _mark_occurrence_snapshot_conflict(
        db: aiosqlite.Connection,
        occurrence_id: str,
    ) -> ReminderMaterializationResult:
        cursor = await db.execute(
            """UPDATE reminder_occurrences
               SET snapshot_conflict = 1
               WHERE occurrence_id = ? AND snapshot_conflict = 0""",
            (occurrence_id,),
        )
        await db.commit()
        return ReminderMaterializationResult(cursor.rowcount == 1, conflict=True)

    @staticmethod
    async def _mark_reminder_snapshot_conflict(
        db: aiosqlite.Connection,
        reminder_id: str,
    ) -> ReminderMaterializationResult:
        cursor = await db.execute(
            """UPDATE reminder_occurrences
               SET snapshot_conflict = 1
               WHERE reminder_id = ? AND snapshot_conflict = 0""",
            (reminder_id,),
        )
        if cursor.rowcount:
            await db.commit()
            return ReminderMaterializationResult(True, conflict=True)
        await db.rollback()
        return ReminderMaterializationResult(False)

    async def materialize_remote_scheduled_reminder(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
        opaque_payload: bytes,
    ) -> ReminderMaterializationResult:
        """Materialize a validated Server scheduled reminder without regression."""
        self._validate_remote_scheduled_reminder(
            reminder_id=reminder_id,
            occurrence_id=occurrence_id,
            target=target,
            content=content,
            intended_at_ms=intended_at_ms,
            lifecycle_at_ms=lifecycle_at_ms,
            revision=revision,
            payload_format=payload_format,
            opaque_payload=opaque_payload,
        )
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db,
                    occurrence_id,
                )
                if record is not None:
                    # A locally durable terminal fact wins over a stale
                    # scheduled snapshot. It is neither resurrected nor
                    # fenced by independently encrypted stale payload bytes.
                    if record.state not in {"scheduled", "claimed"}:
                        await db.rollback()
                        return ReminderMaterializationResult(False)
                    if not self._scheduled_snapshot_matches(
                        record,
                        reminder_id=reminder_id,
                        target=target,
                        content=content,
                        intended_at_ms=intended_at_ms,
                        payload_format=payload_format,
                        opaque_payload=opaque_payload,
                    ):
                        return await self._mark_occurrence_snapshot_conflict(
                            db, occurrence_id
                        )
                    ack = min(revision, record.revision)
                    cursor = await db.execute(
                        """UPDATE reminder_occurrences
                           SET server_ack_revision = MAX(server_ack_revision, ?),
                               snapshot_conflict = 0
                           WHERE occurrence_id = ?
                             AND (server_ack_revision < ?
                                  OR snapshot_conflict != 0)""",
                        (ack, occurrence_id, ack),
                    )
                    changed = cursor.rowcount == 1
                    await db.commit()
                    return ReminderMaterializationResult(changed)

                async with db.execute(
                    "SELECT occurrence_id FROM reminder_occurrences WHERE reminder_id = ?",
                    (reminder_id,),
                ) as cursor:
                    same_reminder = await cursor.fetchone()
                if same_reminder is not None:
                    return await self._mark_reminder_snapshot_conflict(
                        db, reminder_id
                    )
                await db.execute(
                    """INSERT INTO reminder_occurrences
                       (reminder_id, occurrence_id, target, content, intended_at_ms,
                        state, created_at_ms, revision, server_ack_revision,
                        payload_format, opaque_payload)
                       VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)""",
                    (
                        reminder_id,
                        occurrence_id,
                        target,
                        content,
                        intended_at_ms,
                        lifecycle_at_ms,
                        revision,
                        revision,
                        payload_format,
                        opaque_payload,
                    ),
                )
                await db.commit()
                return ReminderMaterializationResult(True)
            except BaseException:
                await db.rollback()
                raise

    async def materialize_remote_terminal_reminder(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        intended_at_ms: int,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
    ) -> ReminderMaterializationResult:
        """Apply an authenticated Server terminal reminder without an Inbox event."""
        self._validate_remote_terminal_reminder(
            reminder_id=reminder_id,
            occurrence_id=occurrence_id,
            intended_at_ms=intended_at_ms,
            lifecycle=lifecycle,
            lifecycle_at_ms=lifecycle_at_ms,
            revision=revision,
            payload_format=payload_format,
        )
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db,
                    occurrence_id,
                )
                if record is None:
                    return await self._mark_reminder_snapshot_conflict(
                        db, reminder_id
                    )
                if (
                    record.reminder_id != reminder_id
                    or record.intended_at_ms != intended_at_ms
                    or record.payload_format not in {None, payload_format}
                ):
                    return await self._mark_occurrence_snapshot_conflict(
                        db, occurrence_id
                    )

                (
                    next_state,
                    actual_fire_at_ms,
                    cancelled_at_ms,
                    delivered_at_ms,
                    delivered_event_id,
                    next_revision,
                    next_ack_revision,
                ) = self._merged_remote_terminal_values(
                    record,
                    lifecycle,
                    lifecycle_at_ms,
                    revision,
                )
                if not self._terminal_materialization_changes(
                    record,
                    (
                        next_state,
                        actual_fire_at_ms,
                        cancelled_at_ms,
                        delivered_at_ms,
                        delivered_event_id,
                        next_revision,
                        next_ack_revision,
                    ),
                ):
                    await db.rollback()
                    return ReminderMaterializationResult(False)
                await db.execute(
                    """UPDATE reminder_occurrences
                       SET state = ?, claimed_at_ms = NULL,
                           actual_fire_at_ms = ?, cancelled_at_ms = ?,
                           delivered_at_ms = ?, delivered_event_id = ?,
                           revision = ?, server_ack_revision = ?,
                           payload_format = COALESCE(payload_format, ?),
                           sync_retry_after_ms = NULL, sync_retry_count = 0,
                           sync_permanent_revision = NULL,
                           sync_permanent_code = NULL,
                           delivery_claim_id = NULL, delivery_claim_acquired = 0,
                           delivery_claim_expires_at_ms = NULL, snapshot_conflict = 0
                       WHERE occurrence_id = ?""",
                    (
                        next_state,
                        actual_fire_at_ms,
                        cancelled_at_ms,
                        delivered_at_ms,
                        delivered_event_id,
                        next_revision,
                        next_ack_revision,
                        payload_format,
                        occurrence_id,
                    ),
                )
                await db.commit()
                return ReminderMaterializationResult(True)
            except BaseException:
                await db.rollback()
                raise

    async def list_reminders(
        self,
        *,
        state: str = "",
        limit: int = 50,
    ) -> tuple[ReminderOccurrence, ...]:
        if not isinstance(state, str) or (state and state not in REMINDER_STATES):
            raise ValueError("state must be empty or a reminder state")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_REMINDER_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REMINDER_LIST_LIMIT}")
        async with self._inbox_lock:
            db = await self._ensure_db()
            if state == "scheduled":
                query = (
                    "SELECT * FROM reminder_occurrences "
                    "WHERE state IN ('scheduled', 'claimed') "
                    "ORDER BY intended_at_ms, occurrence_id LIMIT ?"
                )
                params: tuple[Any, ...] = (limit,)
            elif state:
                query = (
                    "SELECT * FROM reminder_occurrences WHERE state = ? "
                    "ORDER BY intended_at_ms, occurrence_id LIMIT ?"
                )
                params = (state, limit)
            else:
                query = (
                    "SELECT * FROM reminder_occurrences "
                    "ORDER BY intended_at_ms, occurrence_id LIMIT ?"
                )
                params = (limit,)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
            return tuple(self._reminder_from_row(row) for row in rows)

    async def next_reminder_deadline(
        self,
        *,
        now_ms: int | None = None,
        local_only: bool = False,
        after_ms: int | None = None,
    ) -> int | None:
        """Return the next eligible deadline, or now for claimed recovery.

        ``after_ms`` restricts the answer to work that is not yet due.  A
        caller that already owns a wake for the due window — a delivery-claim
        retry — asks that way so a blocked due row cannot mask an independent
        occurrence scheduled before that retry.  Claimed recovery is due work
        by definition, so it is skipped for that question rather than
        reported as ``now``.
        """
        now = int(self._now_ms()) if now_ms is None else now_ms
        if not self._valid_reminder_time(now):
            raise ValueError("now_ms must be an integer epoch milliseconds")
        if after_ms is not None and not self._valid_reminder_time(after_ms):
            raise ValueError("after_ms must be an integer epoch milliseconds")
        async with self._inbox_lock:
            db = await self._ensure_db()
            if after_ms is None:
                async with db.execute(
                    f"""SELECT 1 FROM reminder_occurrences
                        WHERE state = 'claimed'
                          AND snapshot_conflict = 0
                          AND (? = 0 OR {_REMINDER_LOCAL_DELIVERY_SQL})
                        LIMIT 1""",
                    (1 if local_only else 0,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        return now
            async with db.execute(
                f"""SELECT intended_at_ms FROM reminder_occurrences
                    WHERE state = 'scheduled'
                      AND snapshot_conflict = 0
                      AND (? = 0 OR {_REMINDER_LOCAL_DELIVERY_SQL})
                      AND (? IS NULL OR intended_at_ms > ?)
                    ORDER BY intended_at_ms, occurrence_id LIMIT 1""",
                (1 if local_only else 0, after_ms, after_ms),
            ) as cursor:
                row = await cursor.fetchone()
            return int(row["intended_at_ms"]) if row is not None else None

    async def claim_due_reminders(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[ReminderOccurrence, ...]:
        """Claim every due scheduled occurrence without changing its identity."""
        now = int(self._now_ms()) if now_ms is None else now_ms
        if not self._valid_reminder_time(now):
            raise ValueError("now_ms must be an integer epoch milliseconds")
        async with self._inbox_lock:
            return await self._claim_due_reminders_unlocked(now)

    async def due_reminder_delivery_candidates(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[ReminderSyncRecord, ...]:
        """Return due work for a remote delivery authorizer without claiming it."""
        now = int(self._now_ms()) if now_ms is None else now_ms
        if not self._valid_reminder_time(now):
            raise ValueError("now_ms must be an integer epoch milliseconds")
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                """SELECT * FROM reminder_occurrences
                   WHERE snapshot_conflict = 0
                     AND ((state = 'scheduled' AND intended_at_ms <= ?)
                          OR state = 'claimed')
                   ORDER BY intended_at_ms, occurrence_id""",
                (now,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(self._reminder_sync_record_from_row(row) for row in rows)

    async def persist_reminder_delivery_claim_id(
        self,
        *,
        occurrence_id: str,
        revision: int,
        claim_id: str,
    ) -> ReminderSyncRecord | None:
        """Persist an idempotency key before its remote delivery-claim POST."""
        if (
            not isinstance(occurrence_id, str)
            or not occurrence_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
            or not isinstance(claim_id, str)
            or not claim_id
        ):
            raise ValueError("invalid reminder delivery claim")
        try:
            uuid.UUID(claim_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid reminder delivery claim") from exc
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db, occurrence_id
                )
                if record is None or record.revision != revision:
                    await db.rollback()
                    return None
                if record.delivery_claim_id is not None:
                    await db.rollback()
                    return record
                await db.execute(
                    """UPDATE reminder_occurrences SET delivery_claim_id = ?
                       WHERE occurrence_id = ? AND revision = ?
                         AND delivery_claim_id IS NULL
                         AND snapshot_conflict = 0""",
                    (claim_id, occurrence_id, revision),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            return await self._get_reminder_sync_by_occurrence_unlocked(
                db, occurrence_id
            )

    async def acquire_reminder_delivery_claim(
        self,
        *,
        occurrence_id: str,
        revision: int,
        claim_id: str,
        lease_expires_at_ms: int,
    ) -> bool:
        """Record Server-acquired delivery custody for one current revision."""
        if not self._valid_reminder_time(lease_expires_at_ms):
            raise ValueError("invalid reminder delivery claim expiry")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """UPDATE reminder_occurrences
                       SET delivery_claim_acquired = 1,
                           delivery_claim_expires_at_ms = ?
                       WHERE occurrence_id = ? AND revision = ?
                         AND delivery_claim_id = ?
                         AND state IN ('scheduled', 'claimed')
                         AND snapshot_conflict = 0""",
                    (lease_expires_at_ms, occurrence_id, revision, claim_id),
                )
                changed = cursor.rowcount == 1
                await db.commit()
                return changed
            except BaseException:
                await db.rollback()
                raise

    async def claim_authorized_reminders(
        self,
        occurrence_ids: tuple[str, ...],
        *,
        now_ms: int,
    ) -> tuple[ReminderOccurrence, ...]:
        """Enter local claimed state only for IDs that still have custody."""
        if not occurrence_ids:
            return ()
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ", ".join("?" for _ in occurrence_ids)
                claim_custody_now_ms = int(self._now_ms())
                await db.execute(
                    f"""UPDATE reminder_occurrences
                        SET state = 'claimed', claimed_at_ms = ?, actual_fire_at_ms = ?
                        WHERE occurrence_id IN ({placeholders})
                          AND state = 'scheduled'
                          AND {_reminder_delivery_custody_sql()}""",
                    (now_ms, now_ms, *occurrence_ids, claim_custody_now_ms),
                )
                select_custody_now_ms = int(self._now_ms())
                async with db.execute(
                    f"""SELECT * FROM reminder_occurrences
                        WHERE occurrence_id IN ({placeholders})
                          AND state = 'claimed'
                          AND {_reminder_delivery_custody_sql()}
                        ORDER BY intended_at_ms, occurrence_id""",
                    (*occurrence_ids, select_custody_now_ms),
                ) as cursor:
                    rows = await cursor.fetchall()
                await db.commit()
                return tuple(self._reminder_from_row(row) for row in rows)
            except BaseException:
                await db.rollback()
                raise

    async def _claim_due_reminders_unlocked(
        self,
        now_ms: int,
    ) -> tuple[ReminderOccurrence, ...]:
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                f"""SELECT reminder_id FROM reminder_occurrences
                    WHERE state = 'scheduled' AND intended_at_ms <= ?
                      AND {_REMINDER_LOCAL_DELIVERY_SQL}
                    ORDER BY intended_at_ms, occurrence_id""",
                (now_ms,),
            ) as cursor:
                rows = await cursor.fetchall()
            claimed_ids: list[str] = []
            for row in rows:
                reminder_id = str(row["reminder_id"])
                cursor = await db.execute(
                    f"""UPDATE reminder_occurrences
                        SET state = 'claimed', claimed_at_ms = ?, actual_fire_at_ms = ?
                        WHERE reminder_id = ? AND state = 'scheduled'
                          AND {_REMINDER_LOCAL_DELIVERY_SQL}""",
                    (now_ms, now_ms, reminder_id),
                )
                if cursor.rowcount == 1:
                    claimed_ids.append(reminder_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        claimed: list[ReminderOccurrence] = []
        for reminder_id in claimed_ids:
            occurrence = await self._get_reminder_unlocked(db, reminder_id)
            assert occurrence is not None
            claimed.append(occurrence)
        return tuple(claimed)

    @staticmethod
    def _reminder_event_payload(reminder: ReminderOccurrence) -> dict[str, Any]:
        kind, space_id, channel_id, tail = parse_reminder_target(reminder.target)
        actual_fire_at_ms = reminder.actual_fire_at_ms
        if actual_fire_at_ms is None:
            raise LifecycleConflict("claimed reminder is missing actual fire time")
        content = {
            "event_type": "reminder",
            "reminder_id": reminder.reminder_id,
            "occurrence_id": reminder.occurrence_id,
            "target": reminder.target,
            "content": reminder.content,
            "intended_at": reminder_time_to_rfc3339(reminder.intended_at_ms),
            "actual_fire_at": reminder_time_to_rfc3339(actual_fire_at_ms),
        }
        if kind == "dm":
            return {
                "envelope_id": f"reminder-occurrence:{reminder.occurrence_id}",
                "envelope_kind": "dm",
                # ``target_projection`` and ``route_for`` use this peer.
                "sender_slug": tail,
                "recipient_slug": "",
                "content_type": "application/puffo-reminder+json",
                "content": content,
                "sent_at": actual_fire_at_ms,
            }
        return {
            "envelope_id": f"reminder-occurrence:{reminder.occurrence_id}",
            "envelope_kind": "channel",
            "sender_slug": "reminder",
            "channel_id": channel_id,
            "space_id": space_id,
            "content_type": "application/puffo-reminder+json",
            "content": content,
            "sent_at": actual_fire_at_ms,
            "thread_root_id": tail or None,
        }

    async def _deliver_claimed_reminder_unlocked(
        self,
        reminder_id: str,
        *,
        now_ms: int,
    ) -> ReminderOccurrence | None:
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            select_custody_now_ms = int(self._now_ms())
            async with db.execute(
                f"""SELECT * FROM reminder_occurrences
                    WHERE reminder_id = ? AND state = 'claimed'
                      AND {_reminder_delivery_custody_sql()}""",
                (reminder_id, select_custody_now_ms),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            reminder = self._reminder_from_row(row)
            event_id = f"reminder-occurrence:{reminder.occurrence_id}"
            async with db.execute(
                "SELECT receipt_disposition, content FROM messages WHERE envelope_id = ?",
                (event_id,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is None:
                payload = self._reminder_event_payload(reminder)
                await self._insert_local_event_in_transaction(
                    db,
                    values=self._payload_values(payload, now_ms),
                    reason="reminder occurrence",
                )
            else:
                # This state is impossible after a normal crash because the
                # insert and terminal occurrence update commit together.  If
                # a pre-existing row is encountered, fail closed unless it is
                # exactly a local reminder event for this occurrence.
                try:
                    existing_content = json.loads(existing["content"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_content = None
                if (
                    existing["receipt_disposition"]
                    != ReceiptDisposition.LOCAL_RUNTIME.value
                    or not isinstance(existing_content, dict)
                    or existing_content.get("event_type") != "reminder"
                    or existing_content.get("occurrence_id") != reminder.occurrence_id
                ):
                    raise LifecycleConflict(
                        "reminder delivery event identity belongs to another row"
                    )
            final_custody_now_ms = int(self._now_ms())
            cursor = await db.execute(
                f"""UPDATE reminder_occurrences
                   SET state = 'delivered', delivered_event_id = ?, delivered_at_ms = ?,
                       revision = revision + 1,
                       sync_retry_after_ms = NULL,
                       sync_retry_count = 0,
                       sync_permanent_revision = NULL,
                       sync_permanent_code = NULL
                   WHERE reminder_id = ? AND state = 'claimed'
                     AND {_reminder_delivery_custody_sql()}""",
                (event_id, now_ms, reminder_id, final_custody_now_ms),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        delivered = await self._get_reminder_unlocked(db, reminder_id)
        assert delivered is not None
        return delivered

    async def deliver_due_reminders(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[ReminderOccurrence, ...]:
        """Atomically enqueue every claimed/due occurrence at most once.

        A claimed occurrence is included even when it was claimed by a worker
        that restarted before delivery.  Every individual delivery commits the
        local Inbox row and terminal occurrence state together.
        """
        now = int(self._now_ms()) if now_ms is None else now_ms
        if not self._valid_reminder_time(now):
            raise ValueError("now_ms must be an integer epoch milliseconds")
        async with self._inbox_lock:
            await self._claim_due_reminders_unlocked(now)
            db = await self._ensure_db()
            async with db.execute(
                f"""SELECT reminder_id FROM reminder_occurrences
                    WHERE state = 'claimed' AND {_REMINDER_LOCAL_DELIVERY_SQL}
                    ORDER BY intended_at_ms, occurrence_id"""
            ) as cursor:
                rows = await cursor.fetchall()
            delivered: list[ReminderOccurrence] = []
            for row in rows:
                occurrence = await self._deliver_claimed_reminder_unlocked(
                    str(row["reminder_id"]),
                    now_ms=now,
                )
                if occurrence is not None:
                    delivered.append(occurrence)
            return tuple(delivered)

    async def deliver_authorized_reminders(
        self,
        occurrence_ids: tuple[str, ...],
        *,
        now_ms: int,
    ) -> tuple[ReminderOccurrence, ...]:
        """Use the Inbox-plus-terminal transaction only for IDs with custody."""
        if not occurrence_ids:
            return ()
        async with self._inbox_lock:
            db = await self._ensure_db()
            placeholders = ", ".join("?" for _ in occurrence_ids)
            custody_now_ms = int(self._now_ms())
            async with db.execute(
                f"""SELECT reminder_id FROM reminder_occurrences
                    WHERE occurrence_id IN ({placeholders})
                      AND state = 'claimed'
                      AND {_reminder_delivery_custody_sql()}
                    ORDER BY intended_at_ms, occurrence_id""",
                (*occurrence_ids, custody_now_ms),
            ) as cursor:
                rows = await cursor.fetchall()
            delivered: list[ReminderOccurrence] = []
            for row in rows:
                occurrence = await self._deliver_claimed_reminder_unlocked(
                    str(row["reminder_id"]),
                    now_ms=now_ms,
                )
                if occurrence is not None:
                    delivered.append(occurrence)
            return tuple(delivered)

    @staticmethod
    def _replacement_terminal_fields(
        lifecycle: str,
        lifecycle_at_ms: int,
    ) -> tuple[int | None, int | None, int | None]:
        if lifecycle == "scheduled":
            return None, None, None
        if lifecycle == "cancelled":
            return None, lifecycle_at_ms, None
        if lifecycle == "delivered":
            return lifecycle_at_ms, None, lifecycle_at_ms
        raise ValueError("invalid replacement lifecycle")

    @staticmethod
    def _replacement_matches(
        record: ReminderSyncRecord,
        *,
        reminder_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        payload_format: str | None,
        opaque_payload: bytes | None,
    ) -> bool:
        return (
            record.reminder_id == reminder_id
            and record.target == target
            and record.content == content
            and record.intended_at_ms == intended_at_ms
            and record.payload_format == payload_format
            and record.opaque_payload == opaque_payload
        )

    async def replace_local_reminder(
        self,
        *,
        reminder_id: str,
        replacement_reminder_id: str,
        replacement_occurrence_id: str,
        content: str,
        target: str,
        intended_at_ms: int,
        replaced_at_ms: int,
    ) -> tuple[ReminderOccurrence, ReminderOccurrence]:
        """Atomically replace work that has never crossed the remote boundary."""
        parse_reminder_target(target)
        if (
            not content
            or reminder_plaintext_envelope_size(target, content)
            > MAX_REMINDER_ENVELOPE_BYTES
            or not self._valid_reminder_time(intended_at_ms)
            or not self._valid_reminder_time(replaced_at_ms)
        ):
            raise ValueError("invalid reminder replacement")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                old = await self._get_reminder_unlocked(db, reminder_id)
                if old is None:
                    raise ValueError("unknown reminder_id")
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db, old.occurrence_id
                )
                assert record is not None
                existing = await self._get_reminder_sync_by_occurrence_unlocked(
                    db, replacement_occurrence_id
                )
                if old.state == "cancelled" and existing is not None:
                    if not self._replacement_matches(
                        existing,
                        reminder_id=replacement_reminder_id,
                        target=target,
                        content=content,
                        intended_at_ms=intended_at_ms,
                        payload_format=None,
                        opaque_payload=None,
                    ):
                        raise LifecycleConflict("replacement identity changed")
                    await db.rollback()
                    return old, self._reminder_from_sync_record(existing)
                if existing is not None:
                    raise LifecycleConflict("replacement identity already exists")
                if old.state != "scheduled" or not record.is_unprepared_local_only:
                    raise LifecycleConflict(
                        "reminder replacement conflicts with claimed or remote work"
                    )
                await db.execute(
                    """INSERT INTO reminder_occurrences
                       (reminder_id, occurrence_id, target, content, intended_at_ms,
                        state, created_at_ms, revision, server_ack_revision)
                       VALUES (?, ?, ?, ?, ?, 'scheduled', ?, 1, 0)""",
                    (
                        replacement_reminder_id,
                        replacement_occurrence_id,
                        target,
                        content,
                        intended_at_ms,
                        replaced_at_ms,
                    ),
                )
                await db.execute(
                    """UPDATE reminder_occurrences
                       SET state = 'cancelled', cancelled_at_ms = ?, revision = 2,
                           sync_retry_after_ms = NULL, sync_retry_count = 0
                       WHERE reminder_id = ? AND state = 'scheduled'""",
                    (replaced_at_ms, reminder_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            cancelled = await self._get_reminder_unlocked(db, reminder_id)
            replacement = await self._get_reminder_unlocked(
                db, replacement_reminder_id
            )
            assert cancelled is not None and replacement is not None
            return cancelled, replacement

    @staticmethod
    def _reminder_from_sync_record(record: ReminderSyncRecord) -> ReminderOccurrence:
        return ReminderOccurrence(
            reminder_id=record.reminder_id,
            occurrence_id=record.occurrence_id,
            target=record.target,
            content=record.content,
            intended_at_ms=record.intended_at_ms,
            state=record.state,
            created_at_ms=record.created_at_ms,
            claimed_at_ms=record.claimed_at_ms,
            actual_fire_at_ms=record.actual_fire_at_ms,
            cancelled_at_ms=record.cancelled_at_ms,
            delivered_at_ms=record.delivered_at_ms,
            delivered_event_id=record.delivered_event_id,
        )

    def _validate_remote_replacement_fields(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        created_at_ms: int,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
        opaque_payload: bytes,
    ) -> tuple[int | None, int | None, int | None]:
        self._validate_remote_scheduled_reminder(
            reminder_id=reminder_id,
            occurrence_id=occurrence_id,
            target=target,
            content=content,
            intended_at_ms=intended_at_ms,
            lifecycle_at_ms=lifecycle_at_ms,
            revision=revision,
            payload_format=payload_format,
            opaque_payload=opaque_payload,
        )
        if not self._valid_reminder_time(created_at_ms):
            raise ValueError("invalid replacement creation time")
        if revision != (1 if lifecycle == "scheduled" else 2):
            raise ValueError("invalid replacement lifecycle revision")
        return self._replacement_terminal_fields(lifecycle, lifecycle_at_ms)

    async def _upsert_remote_replacement_unlocked(
        self,
        db: aiosqlite.Connection,
        *,
        reminder_id: str,
        occurrence_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        created_at_ms: int,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
        opaque_payload: bytes,
        actual_fire_at_ms: int | None,
        cancelled_at_ms: int | None,
        delivered_at_ms: int | None,
    ) -> None:
        replacement = await self._get_reminder_sync_by_occurrence_unlocked(
            db, occurrence_id
        )
        if replacement is None:
            await db.execute(
                """INSERT INTO reminder_occurrences
                   (reminder_id, occurrence_id, target, content, intended_at_ms,
                    state, created_at_ms, actual_fire_at_ms, cancelled_at_ms,
                    delivered_at_ms, revision, server_ack_revision,
                    payload_format, opaque_payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reminder_id,
                    occurrence_id,
                    target,
                    content,
                    intended_at_ms,
                    lifecycle,
                    created_at_ms,
                    actual_fire_at_ms,
                    cancelled_at_ms,
                    delivered_at_ms,
                    revision,
                    revision,
                    payload_format,
                    opaque_payload,
                ),
            )
            return
        if not self._replacement_matches(
            replacement,
            reminder_id=reminder_id,
            target=target,
            content=content,
            intended_at_ms=intended_at_ms,
            payload_format=payload_format,
            opaque_payload=opaque_payload,
        ):
            raise LifecycleConflict("replacement identity changed")
        if replacement.state in {"scheduled", "claimed"} and lifecycle != "scheduled":
            await db.execute(
                """UPDATE reminder_occurrences
                   SET state = ?, actual_fire_at_ms = ?, cancelled_at_ms = ?,
                       delivered_at_ms = ?, revision = ?, server_ack_revision = ?,
                       claimed_at_ms = NULL, delivery_claim_id = NULL,
                       delivery_claim_acquired = 0,
                       delivery_claim_expires_at_ms = NULL,
                       sync_retry_after_ms = NULL, sync_retry_count = 0,
                       sync_permanent_revision = NULL,
                       sync_permanent_code = NULL, snapshot_conflict = 0
                   WHERE occurrence_id = ? AND state IN ('scheduled', 'claimed')""",
                (
                    lifecycle,
                    actual_fire_at_ms,
                    cancelled_at_ms,
                    delivered_at_ms,
                    revision,
                    revision,
                    occurrence_id,
                ),
            )

    @staticmethod
    async def _cancel_replaced_occurrence_unlocked(
        db: aiosqlite.Connection,
        occurrence_id: str,
        cancelled_at_ms: int,
    ) -> None:
        await db.execute(
            """UPDATE reminder_occurrences
               SET state = CASE
                       WHEN state = 'delivered' THEN state
                       ELSE 'cancelled'
                   END,
                   claimed_at_ms = NULL,
                   actual_fire_at_ms = CASE
                       WHEN state = 'delivered' THEN actual_fire_at_ms
                       ELSE NULL
                   END,
                   cancelled_at_ms = CASE
                       WHEN state = 'delivered' THEN cancelled_at_ms
                       ELSE ?
                   END,
                   delivered_at_ms = CASE
                       WHEN state = 'delivered' THEN delivered_at_ms
                       ELSE NULL
                   END,
                   delivered_event_id = CASE
                       WHEN state = 'delivered' THEN delivered_event_id
                       ELSE NULL
                   END,
                   revision = MAX(revision, 2),
                   server_ack_revision = MAX(server_ack_revision, 2),
                   delivery_claim_id = NULL, delivery_claim_acquired = 0,
                   delivery_claim_expires_at_ms = NULL,
                   sync_retry_after_ms = NULL, sync_retry_count = 0,
                   sync_permanent_revision = NULL,
                   sync_permanent_code = NULL, snapshot_conflict = 0
               WHERE occurrence_id = ?""",
            (cancelled_at_ms, occurrence_id),
        )

    async def materialize_remote_replacement(
        self,
        *,
        reminder_id: str,
        occurrence_id: str,
        cancelled_at_ms: int,
        replacement_reminder_id: str,
        replacement_occurrence_id: str,
        target: str,
        content: str,
        intended_at_ms: int,
        replacement_created_at_ms: int,
        lifecycle: str,
        lifecycle_at_ms: int,
        revision: int,
        payload_format: str,
        opaque_payload: bytes,
    ) -> tuple[ReminderOccurrence, ReminderOccurrence]:
        """Commit one authenticated Server replacement to SQLite atomically."""
        fields = self._validate_remote_replacement_fields(
            reminder_id=replacement_reminder_id,
            occurrence_id=replacement_occurrence_id,
            target=target,
            content=content,
            intended_at_ms=intended_at_ms,
            created_at_ms=replacement_created_at_ms,
            lifecycle=lifecycle,
            lifecycle_at_ms=lifecycle_at_ms,
            revision=revision,
            payload_format=payload_format,
            opaque_payload=opaque_payload,
        )
        actual_fire_at_ms, replacement_cancelled_at_ms, delivered_at_ms = fields
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                old = await self._get_reminder_sync_by_occurrence_unlocked(
                    db, occurrence_id
                )
                if old is None or old.reminder_id != reminder_id:
                    raise LifecycleConflict("reminder replacement identity changed")
                if old.state not in {
                    "scheduled", "claimed", "cancelled", "delivered",
                }:
                    raise LifecycleConflict("invalid reminder lifecycle")
                await self._upsert_remote_replacement_unlocked(
                    db,
                    reminder_id=replacement_reminder_id,
                    occurrence_id=replacement_occurrence_id,
                    target=target,
                    content=content,
                    intended_at_ms=intended_at_ms,
                    created_at_ms=replacement_created_at_ms,
                    lifecycle=lifecycle,
                    lifecycle_at_ms=lifecycle_at_ms,
                    revision=revision,
                    payload_format=payload_format,
                    opaque_payload=opaque_payload,
                    actual_fire_at_ms=actual_fire_at_ms,
                    cancelled_at_ms=replacement_cancelled_at_ms,
                    delivered_at_ms=delivered_at_ms,
                )
                await self._cancel_replaced_occurrence_unlocked(
                    db, occurrence_id, cancelled_at_ms
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            cancelled = await self._get_reminder_unlocked(db, reminder_id)
            created = await self._get_reminder_unlocked(db, replacement_reminder_id)
            assert cancelled is not None and created is not None
            return cancelled, created

    async def cancel_reminder(
        self,
        reminder_id: str,
        *,
        cancelled_at_ms: int | None = None,
    ) -> ReminderOccurrence:
        """Idempotently cancel work that has not terminally delivered."""
        if not isinstance(reminder_id, str) or not reminder_id:
            raise ValueError("reminder_id must be a non-empty string")
        cancelled_at = (
            int(self._now_ms()) if cancelled_at_ms is None else cancelled_at_ms
        )
        if not self._valid_reminder_time(cancelled_at):
            raise ValueError("cancelled_at_ms must be an integer epoch milliseconds")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                reminder = await self._get_reminder_unlocked(db, reminder_id)
                if reminder is None:
                    raise ValueError("unknown reminder_id")
                record = await self._get_reminder_sync_by_occurrence_unlocked(
                    db,
                    reminder.occurrence_id,
                )
                assert record is not None
                if (
                    reminder.state in {"scheduled", "claimed"}
                    and not record.is_unprepared_local_only
                ):
                    raise LifecycleConflict(
                        "reminder cancellation conflicts with claimed or remote work"
                    )
                if reminder.state in {"scheduled", "claimed"}:
                    cursor = await db.execute(
                        """UPDATE reminder_occurrences
                           SET state = 'cancelled', cancelled_at_ms = ?,
                               claimed_at_ms = NULL, actual_fire_at_ms = NULL,
                               revision = revision + 1,
                               delivery_claim_id = NULL,
                               delivery_claim_acquired = 0,
                               delivery_claim_expires_at_ms = NULL,
                               sync_retry_after_ms = NULL,
                               sync_retry_count = 0,
                               sync_permanent_revision = NULL,
                               sync_permanent_code = NULL
                           WHERE reminder_id = ? AND state IN ('scheduled', 'claimed')""",
                        (cancelled_at, reminder_id),
                    )
                    if cursor.rowcount != 1:
                        raise LifecycleConflict("reminder cancellation lost ownership")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            result = await self._get_reminder_unlocked(db, reminder_id)
            assert result is not None
            return result
