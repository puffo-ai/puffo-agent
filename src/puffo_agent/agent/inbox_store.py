"""Durable Inbox scheduling, admission, and turn lifecycle operations."""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable

import aiosqlite

from .message_projection import canonical_target_parts
from .message_store_models import (
    PRIOR_CONTEXT_MAX_BYTES,
    PRIOR_CONTEXT_MAX_ITEMS,
    InboxNoticeState,
    InboxPage,
    LifecycleConflict,
    PendingPage,
    PriorContextPage,
    ProcessingState,
    ReceiptDisposition,
    StoredMessage,
    TurnRun,
    _now_ms,
)

_SQLITE_ID_BATCH_SIZE = 500


async def _batched_select_ids(
    db,
    sql_template: str,
    ids: tuple[str, ...],
    *,
    params_after: tuple = (),
) -> set[str]:
    """Run a first-column SELECT over ``ids`` in ``{ids}``-placeholder batches."""
    found: set[str] = set()
    for start in range(0, len(ids), _SQLITE_ID_BATCH_SIZE):
        batch = ids[start:start + _SQLITE_ID_BATCH_SIZE]
        sql = sql_template.format(ids=",".join("?" for _ in batch))
        async with db.execute(sql, (*batch, *params_after)) as cursor:
            found.update(row[0] for row in await cursor.fetchall())
    return found


class InboxStoreMixin:
    """Implementation trait for the single ``MessageStore`` state owner.

    This class has no independent lifecycle and is not a reusable store API.
    ``MessageStore`` owns its connection, locks, and public compatibility
    surface; the trait only keeps that facade within the repository size limit.
    """

    async def _migrate_nullable_provider_session(
        self, db: aiosqlite.Connection
    ) -> None:
        """Upgrade databases created by the first Package 1 implementation."""
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("PRAGMA table_info(turn_runs)") as cursor:
                columns = {row["name"]: row for row in await cursor.fetchall()}
            provider_column = columns.get("provider_session_id")
            if provider_column is None or not provider_column["notnull"]:
                await db.commit()
                return

            await db.execute(
                "ALTER TABLE turn_run_messages "
                "RENAME TO turn_run_messages_nonnullable_migration"
            )
            await db.execute(
                "ALTER TABLE turn_runs RENAME TO turn_runs_nonnullable_migration"
            )
            statements = (
                """CREATE TABLE turn_runs (
                    turn_id TEXT PRIMARY KEY,
                    provider_session_id TEXT,
                    state TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER
                )""",
                """CREATE TABLE turn_run_messages (
                    turn_id TEXT NOT NULL,
                    envelope_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY (turn_id, envelope_id),
                    UNIQUE (turn_id, ordinal),
                    FOREIGN KEY (turn_id) REFERENCES turn_runs(turn_id)
                )""",
                """INSERT INTO turn_runs
                    (turn_id, provider_session_id, state, started_at, completed_at)
                SELECT turn_id, provider_session_id, state, started_at, completed_at
                FROM turn_runs_nonnullable_migration""",
                """INSERT INTO turn_run_messages (turn_id, envelope_id, ordinal)
                SELECT turn_id, envelope_id, ordinal
                FROM turn_run_messages_nonnullable_migration""",
                "DROP TABLE turn_run_messages_nonnullable_migration",
                "DROP TABLE turn_runs_nonnullable_migration",
            )
            for statement in statements:
                await db.execute(statement)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def _migrate_notice_provider_session(self, db: aiosqlite.Connection) -> None:
        """Add the accepting native session without rewriting notice state.

        The first global-Inbox schema stored only a generation-level delivery
        marker.  Existing accepted generations keep that marker and get a
        ``NULL`` session, which makes them eligible for one bounded discovery
        by the next known native session instead of pretending the old
        acceptance belongs to an arbitrary resumed transcript.
        """
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("PRAGMA table_info(inbox_notice_state)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            if "last_delivered_provider_session_id" not in columns:
                await db.execute(
                    "ALTER TABLE inbox_notice_state "
                    "ADD COLUMN last_delivered_provider_session_id TEXT"
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def _refresh_notice_state(
        self,
        db: aiosqlite.Connection,
        *,
        activated_at: int | None = None,
    ) -> InboxNoticeState:
        """Refresh the singleton notice row inside the caller's transaction."""
        async with db.execute(
            "SELECT generation, pending_count, first_pending_deadline_ms, "
            "last_delivered_generation, last_delivered_provider_session_id "
            "FROM inbox_notice_state WHERE singleton = 1"
        ) as cursor:
            old = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE processing_state = ?",
            (ProcessingState.PENDING.value,),
        ) as cursor:
            count_row = await cursor.fetchone()
        pending_count = int(count_row[0])
        old_count = int(old["pending_count"])
        generation = int(old["generation"]) + (pending_count != old_count)
        deadline = old["first_pending_deadline_ms"]
        if pending_count == 0:
            deadline = None
            await db.execute("DELETE FROM inbox_notice_contributions")
        elif old_count == 0:
            started = (
                int(activated_at) if activated_at is not None else int(self._now_ms())
            )
            deadline = started + self.NOTICE_WINDOW_MS
        await db.execute(
            "DELETE FROM inbox_notice_contributions WHERE envelope_id NOT IN "
            "(SELECT envelope_id FROM messages WHERE processing_state = ?)",
            (ProcessingState.PENDING.value,),
        )
        delivered_generation = int(old["last_delivered_generation"])
        delivered_session = old["last_delivered_provider_session_id"]
        if delivered_session and pending_count > 0:
            async with db.execute(
                "SELECT COUNT(*) FROM inbox_notice_contributions "
                "WHERE provider_session_id = ?",
                (delivered_session,),
            ) as cursor:
                contributed = int((await cursor.fetchone())[0])
            if contributed == pending_count:
                delivered_generation = generation
        await db.execute(
            "UPDATE inbox_notice_state SET generation = ?, pending_count = ?, "
            "first_pending_deadline_ms = ?, last_delivered_generation = ? "
            "WHERE singleton = 1",
            (generation, pending_count, deadline, delivered_generation),
        )
        return InboxNoticeState(
            generation,
            pending_count,
            int(deadline) if deadline is not None else None,
            delivered_generation,
            delivered_session,
        )

    async def get_notice_state(self) -> InboxNoticeState:
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT generation, pending_count, first_pending_deadline_ms, "
                "last_delivered_generation, last_delivered_provider_session_id "
                "FROM inbox_notice_state WHERE singleton = 1"
            ) as cursor:
                row = await cursor.fetchone()
            return InboxNoticeState(
                int(row["generation"]),
                int(row["pending_count"]),
                (
                    int(row["first_pending_deadline_ms"])
                    if row["first_pending_deadline_ms"] is not None
                    else None
                ),
                int(row["last_delivered_generation"]),
                row["last_delivered_provider_session_id"],
            )

    @staticmethod
    def _valid_notice_acceptance(
        generation: int,
        provider_session_id: str | None,
    ) -> bool:
        return (
            not isinstance(generation, bool)
            and isinstance(generation, int)
            and generation >= 0
            and isinstance(provider_session_id, str)
            and bool(provider_session_id)
        )

    async def get_notice_snapshot(
        self,
        provider_session_id: str | None,
    ) -> tuple[InboxNoticeState, tuple[StoredMessage, ...]]:
        """Read notice metadata and its session delta from one locked snapshot."""
        async with self._inbox_lock:
            pending = await self._get_pending_unlocked()
            state = await self._notice_state_unlocked()
            if not provider_session_id or not pending:
                return state, pending
            if state.last_delivered_provider_session_id != provider_session_id:
                return state, pending
            db = await self._ensure_db()
            async with db.execute(
                "SELECT envelope_id FROM inbox_notice_contributions "
                "WHERE provider_session_id = ?",
                (provider_session_id,),
            ) as cursor:
                contributed = {row["envelope_id"] for row in await cursor.fetchall()}
            return state, tuple(
                item for item in pending if item.envelope_id not in contributed
            )

    async def get_notice_candidates(
        self,
        provider_session_id: str | None,
    ) -> tuple[StoredMessage, ...]:
        """Return pending rows not yet mentioned to this provider session."""
        _state, candidates = await self.get_notice_snapshot(provider_session_id)
        return candidates

    async def _mark_notice_delivered_unlocked(
        self,
        db: aiosqlite.Connection,
        generation: int,
        provider_session_id: str,
        message_ids: tuple[str, ...],
        *,
        turn_id: str | None = None,
    ) -> bool:
        """Record exact pending identities after one native delivery succeeds."""
        if not message_ids:
            return False
        if turn_id is not None:
            async with db.execute(
                "SELECT 1 FROM turn_runs WHERE turn_id = ? AND state = ? "
                "AND provider_session_id = ?",
                (turn_id, ProcessingState.IN_TURN.value, provider_session_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return False
        state = await self._notice_state_unlocked()
        if generation > state.generation or state.pending_count <= 0:
            return False
        pending_ids = sorted(await _batched_select_ids(
            db,
            "SELECT envelope_id FROM messages WHERE envelope_id IN ({ids}) "
            "AND processing_state = ?",
            message_ids,
            params_after=(ProcessingState.PENDING.value,),
        ))
        if not pending_ids:
            return False
        if state.last_delivered_provider_session_id != provider_session_id:
            await db.execute("DELETE FROM inbox_notice_contributions")
        before = db.total_changes
        await db.executemany(
            "INSERT OR IGNORE INTO inbox_notice_contributions "
            "(envelope_id, provider_session_id) VALUES (?, ?)",
            ((envelope_id, provider_session_id) for envelope_id in pending_ids),
        )
        changed = db.total_changes > before
        async with db.execute(
            "SELECT COUNT(*) FROM inbox_notice_contributions "
            "WHERE provider_session_id = ?",
            (provider_session_id,),
        ) as cursor:
            contributed = int((await cursor.fetchone())[0])
        delivered_generation = (
            state.generation
            if contributed == state.pending_count
            else min(generation, max(0, state.generation - 1))
        )
        await db.execute(
            "UPDATE inbox_notice_state SET last_delivered_generation = ?, "
            "last_delivered_provider_session_id = ? WHERE singleton = 1",
            (delivered_generation, provider_session_id),
        )
        return changed

    async def mark_notice_delivered(
        self,
        generation: int,
        provider_session_id: str | None,
        message_ids: Iterable[str] | None = None,
        *,
        turn_id: str | None = None,
    ) -> bool:
        """Record the exact pending identities accepted by one native session."""
        if not self._valid_notice_acceptance(generation, provider_session_id):
            return False
        assert isinstance(provider_session_id, str)
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                ids = (
                    tuple(message_ids)
                    if message_ids is not None
                    else tuple(
                        item.envelope_id for item in await self._get_pending_unlocked()
                    )
                )
                if not ids or len(ids) != len(set(ids)):
                    await db.rollback()
                    return False
                accepted = await self._mark_notice_delivered_unlocked(
                    db,
                    generation,
                    provider_session_id,
                    ids,
                    turn_id=turn_id,
                )
                await db.commit()
                return accepted
            except Exception:
                await db.rollback()
                raise

    async def release_notice_delivery(
        self,
        provider_session_id: str | None,
        message_ids: Iterable[str] | None = None,
    ) -> bool:
        """Release native-delivery evidence after a transport or runtime failure."""
        if not provider_session_id:
            return False
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                state = await self._notice_state_unlocked()
                if state.last_delivered_provider_session_id != provider_session_id:
                    await db.rollback()
                    return False
                if message_ids is None:
                    cursor = await db.execute(
                        "DELETE FROM inbox_notice_contributions "
                        "WHERE provider_session_id = ?",
                        (provider_session_id,),
                    )
                    deleted = cursor.rowcount
                else:
                    ids = tuple(message_ids)
                    if not ids or len(ids) != len(set(ids)):
                        await db.rollback()
                        return False
                    deleted = 0
                    for offset in range(0, len(ids), _SQLITE_ID_BATCH_SIZE):
                        batch = ids[offset : offset + _SQLITE_ID_BATCH_SIZE]
                        placeholders = ",".join("?" for _ in batch)
                        cursor = await db.execute(
                            "DELETE FROM inbox_notice_contributions "
                            f"WHERE provider_session_id = ? AND envelope_id IN ({placeholders})",
                            (provider_session_id, *batch),
                        )
                        deleted += cursor.rowcount
                if deleted:
                    await db.execute(
                        "UPDATE inbox_notice_state SET last_delivered_generation = "
                        "CASE WHEN generation = 0 THEN 0 ELSE generation - 1 END "
                        "WHERE singleton = 1",
                    )
                await db.commit()
                return bool(deleted)
            except Exception:
                await db.rollback()
                raise

    async def quarantine_pending(self, envelope_id: str, *, reason: str) -> bool:
        async with self._inbox_lock:
            return await self._quarantine_pending_unlocked(envelope_id, reason=reason)

    async def _quarantine_pending_unlocked(
        self, envelope_id: str, *, reason: str
    ) -> bool:
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                """UPDATE messages
                   SET receipt_disposition = ?, receipt_reason = ?,
                       processing_state = NULL, processing_turn_id = NULL,
                       model_visible_at = NULL
                   WHERE envelope_id = ? AND processing_state = ?""",
                (
                    ReceiptDisposition.TERMINAL.value,
                    reason,
                    envelope_id,
                    ProcessingState.PENDING.value,
                ),
            )
            changed = cursor.rowcount == 1
            if changed:
                await self._refresh_notice_state(db)
            await db.commit()
            return changed
        except Exception:
            await db.rollback()
            raise

    async def get_pending(
        self, *, limit: int | None = None
    ) -> tuple[StoredMessage, ...]:
        async with self._inbox_lock:
            return await self._get_pending_unlocked(limit=limit)

    async def _get_pending_unlocked(
        self, *, limit: int | None = None
    ) -> tuple[StoredMessage, ...]:
        db = await self._ensure_db()
        sql = """
            SELECT * FROM messages
            WHERE processing_state = ?
              AND (
                (receipt_disposition = ? AND server_seq IS NOT NULL)
                OR
                (receipt_disposition = ? AND server_seq IS NULL
                 AND local_ordinal IS NOT NULL)
              )
            ORDER BY COALESCE(server_seq, after_server_seq, 0) ASC,
                     CASE WHEN server_seq IS NOT NULL THEN 0 ELSE 1 END ASC,
                     local_ordinal ASC, envelope_id ASC
        """
        params: list[Any] = [
            ProcessingState.PENDING.value,
            ReceiptDisposition.ELIGIBLE.value,
            ReceiptDisposition.LOCAL_RUNTIME.value,
        ]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            sql += " LIMIT ?"
            params.append(limit)
        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return tuple(self._row_to_msg(row) for row in rows)

    @staticmethod
    def target_projection(item: StoredMessage) -> str:
        """Return the canonical Inbox projection for one durable row."""
        target = canonical_target_parts(item)
        if target[0] == "dm":
            return f"dm:{target[1]}"
        base = f"channel:{target[1]}:{target[2]}"
        if target[0] == "thread":
            return f"{base}:thread:{target[3]}"
        return base

    @staticmethod
    def _inbox_order(item: StoredMessage) -> tuple[int, int, int, str]:
        return (
            int(
                item.server_seq
                if item.server_seq is not None
                else item.after_server_seq or 0
            ),
            0 if item.server_seq is not None else 1,
            int(item.local_ordinal or 0),
            item.envelope_id,
        )

    @staticmethod
    def _encode_inbox_cursor(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_inbox_cursor(cursor: str) -> dict[str, Any]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid Inbox cursor") from exc
        if (
            not isinstance(value, dict)
            or value.get("v") != 1
            or not isinstance(value.get("target"), str)
            or not isinstance(value.get("generation"), int)
            or not isinstance(value.get("ceiling"), list)
            or not isinstance(value.get("last"), list)
        ):
            raise ValueError("invalid Inbox cursor")
        return value

    async def read_inbox_page(
        self,
        *,
        target: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> InboxPage:
        """Read one stable pending snapshot without changing lifecycle state."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("limit must be between 1 and 50")
        if not isinstance(target, str):
            raise ValueError("target must be a string")
        target = target.strip()
        if target and not (target.startswith("channel:") or target.startswith("dm:")):
            raise ValueError("invalid Inbox target")
        async with self._inbox_lock:
            decoded: dict[str, Any] | None = None
            if cursor:
                decoded = self._decode_inbox_cursor(cursor)
                cursor_target = decoded["target"]
                if target and cursor_target != target:
                    raise ValueError("Inbox cursor belongs to another target")
                target = cursor_target
            state = await self._notice_state_unlocked()
            pending = tuple(
                item
                for item in await self._get_pending_unlocked()
                if not target or self.target_projection(item) == target
            )
            if decoded is not None:
                generation = int(decoded["generation"])
                ceiling = tuple(decoded["ceiling"])
                last = tuple(decoded["last"])
            else:
                generation = state.generation
                ceiling = self._inbox_order(pending[-1]) if pending else (0, 0, 0, "")
                last = (-1, -1, -1, "")
            snapshot = [
                item for item in pending if last < self._inbox_order(item) <= ceiling
            ]
            selected = tuple(snapshot[:limit])
            remaining = len(snapshot) - len(selected)
            has_more = remaining > 0
            next_cursor = ""
            if has_more:
                next_cursor = self._encode_inbox_cursor(
                    {
                        "v": 1,
                        "target": target,
                        "generation": generation,
                        "ceiling": list(ceiling),
                        "last": list(self._inbox_order(selected[-1])),
                    }
                )
            return InboxPage(
                selected,
                next_cursor,
                has_more,
                remaining,
                generation,
                target,
            )

    async def _notice_state_unlocked(self) -> InboxNoticeState:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT generation, pending_count, first_pending_deadline_ms, "
            "last_delivered_generation, last_delivered_provider_session_id "
            "FROM inbox_notice_state WHERE singleton = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return InboxNoticeState(
            int(row["generation"]),
            int(row["pending_count"]),
            (
                int(row["first_pending_deadline_ms"])
                if row["first_pending_deadline_ms"] is not None
                else None
            ),
            int(row["last_delivered_generation"]),
            row["last_delivered_provider_session_id"],
        )

    async def get_prior_context(
        self,
        anchor: StoredMessage,
        *,
        limit: int = PRIOR_CONTEXT_MAX_ITEMS,
        max_bytes: int = PRIOR_CONTEXT_MAX_BYTES,
    ) -> tuple[StoredMessage, ...]:
        """Return only the bounded rows for compatibility with direct callers."""
        return (
            await self.get_prior_context_page(
                anchor,
                limit=limit,
                max_bytes=max_bytes,
            )
        ).items

    async def get_prior_context_page(
        self,
        anchor: StoredMessage,
        *,
        limit: int = PRIOR_CONTEXT_MAX_ITEMS,
        max_bytes: int = PRIOR_CONTEXT_MAX_BYTES,
    ) -> PriorContextPage:
        from .prior_context import get_prior_context_page

        return await get_prior_context_page(
            self, anchor, limit=limit, max_bytes=max_bytes
        )

    async def get_channel_pending(
        self,
        space_id: str,
        channel_id: str,
        *,
        after_seq: int | None = None,
        through_seq: int | None = None,
        limit: int = 50,
    ) -> PendingPage:
        async with self._inbox_lock:
            return await self._get_channel_pending_unlocked(
                space_id,
                channel_id,
                after_seq=after_seq,
                through_seq=through_seq,
                limit=limit,
            )

    async def _get_channel_pending_unlocked(
        self,
        space_id: str,
        channel_id: str,
        *,
        after_seq: int | None = None,
        through_seq: int | None = None,
        limit: int = 50,
    ) -> PendingPage:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        candidates = [
            item
            for item in await self._get_pending_unlocked()
            if item.space_id == space_id and item.channel_id == channel_id
        ]

        def included(item: StoredMessage) -> bool:
            position = (
                item.server_seq
                if item.server_seq is not None
                else item.after_server_seq
            )
            if position is None:
                position = 0
            if after_seq is not None and position < after_seq:
                return False
            if (
                after_seq is not None
                and position == after_seq
                and item.server_seq is not None
            ):
                return False
            if through_seq is not None and position > through_seq:
                return False
            return True

        filtered = [item for item in candidates if included(item)]
        selected = tuple(filtered[:limit])
        selected_server = [
            item.server_seq for item in selected if item.server_seq is not None
        ]
        return PendingPage(
            selected,
            max(selected_server) if selected_server else None,
            len(filtered) > len(selected),
        )

    @staticmethod
    def _exact_ids(message_ids: Iterable[str]) -> tuple[str, ...]:
        ids = tuple(message_ids)
        if not ids or any(not isinstance(item, str) or not item for item in ids):
            raise LifecycleConflict("message ID set must be non-empty")
        if len(ids) != len(set(ids)):
            raise LifecycleConflict("message ID set contains duplicates")
        return ids

    async def admit_messages(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
        provider_session_id: str | None,
        model_visible_at: int | None = None,
    ) -> TurnRun:
        async with self._inbox_lock:
            return await self._admit_messages_unlocked(
                message_ids,
                turn_id=turn_id,
                provider_session_id=provider_session_id,
                model_visible_at=model_visible_at,
            )

    async def start_turn(
        self,
        *,
        turn_id: str,
        provider_session_id: str | None,
        started_at: int | None = None,
        notice_generation: int | None = None,
        notice_message_ids: Iterable[str] = (),
    ) -> TurnRun:
        """Create an active Turn, atomically claiming a notice when present."""
        if not turn_id:
            raise LifecycleConflict("turn ID is required")
        notice_ids = tuple(notice_message_ids)
        if notice_generation is not None:
            if (
                isinstance(notice_generation, bool)
                or not isinstance(notice_generation, int)
                or notice_generation < 0
                or not notice_ids
                or len(notice_ids) != len(set(notice_ids))
            ):
                raise LifecycleConflict("notice claim requires valid message IDs")
        async with self._inbox_lock:
            db = await self._ensure_db()
            started = started_at if started_at is not None else int(self._now_ms())
            try:
                await db.execute("BEGIN IMMEDIATE")
                if notice_generation is not None and provider_session_id:
                    if not await self._mark_notice_delivered_unlocked(
                        db,
                        notice_generation,
                        provider_session_id,
                        notice_ids,
                    ):
                        raise LifecycleConflict(
                            "Inbox notice is stale or already claimed"
                        )
                await db.execute(
                    "INSERT INTO turn_runs(turn_id, provider_session_id, state, "
                    "started_at, completed_at) VALUES (?, ?, ?, ?, NULL)",
                    (
                        turn_id,
                        provider_session_id,
                        ProcessingState.IN_TURN.value,
                        started,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            run = await self._get_turn_run_unlocked(db, turn_id)
            assert run is not None
            return run

    async def finalize_empty_turn(
        self,
        *,
        turn_id: str,
        state: str = ProcessingState.PROCESSED.value,
    ) -> TurnRun:
        """Finalize a notice Turn which admitted no Inbox rows."""
        if state not in (ProcessingState.PROCESSED.value, "requeued"):
            raise LifecycleConflict("invalid terminal Turn state")
        async with self._inbox_lock:
            db = await self._ensure_db()
            completed = int(self._now_ms())
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE turn_runs SET state = ?, completed_at = ? "
                    "WHERE turn_id = ? AND state = ? AND NOT EXISTS "
                    "(SELECT 1 FROM turn_run_messages WHERE turn_id = ?)",
                    (
                        state,
                        completed,
                        turn_id,
                        ProcessingState.IN_TURN.value,
                        turn_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LifecycleConflict("Turn is not active and empty")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            run = await self._get_turn_run_unlocked(db, turn_id)
            assert run is not None
            return run

    async def transfer_turn_session(
        self,
        turn_id: str,
        *,
        from_provider_session_id: str | None,
        to_provider_session_id: str,
    ) -> TurnRun:
        """Move an active Turn to a replacement provider session atomically.

        A provider retry can land in a *new* session which receives the turn's
        rows as a fresh payload rather than through the original transcript.
        Ownership must then follow the rows durably: this is the only sanctioned
        way ``turn_runs.provider_session_id`` may change, and it succeeds only
        while the turn is still active and still owned by the caller's session.
        """
        if not turn_id:
            raise LifecycleConflict("turn ID is required")
        if not to_provider_session_id:
            raise LifecycleConflict("a replacement provider session is required")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE turn_runs SET provider_session_id = ? "
                    "WHERE turn_id = ? AND state = ? AND provider_session_id IS ?",
                    (
                        to_provider_session_id,
                        turn_id,
                        ProcessingState.IN_TURN.value,
                        from_provider_session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LifecycleConflict(
                        "turn is not active under the previous provider session"
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            run = await self._get_turn_run_unlocked(db, turn_id)
            assert run is not None
            return run

    async def _admit_messages_unlocked(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
        provider_session_id: str | None,
        model_visible_at: int | None = None,
    ) -> TurnRun:
        ids = self._exact_ids(message_ids)
        if not turn_id:
            raise LifecycleConflict("turn ID is required")
        visible_at = model_visible_at if model_visible_at is not None else _now_ms()
        db = await self._ensure_db()
        placeholders = ",".join("?" for _ in ids)
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                f"SELECT envelope_id, processing_state FROM messages "
                f"WHERE envelope_id IN ({placeholders})",
                ids,
            ) as cursor:
                rows = await cursor.fetchall()
            states = {row["envelope_id"]: row["processing_state"] for row in rows}
            if len(states) != len(ids) or any(
                states.get(item) != ProcessingState.PENDING.value for item in ids
            ):
                raise LifecycleConflict("every exact message ID must be pending")
            async with db.execute(
                "SELECT provider_session_id, state FROM turn_runs WHERE turn_id = ?",
                (turn_id,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if existing["provider_session_id"] != provider_session_id:
                    raise LifecycleConflict("turn belongs to another provider session")
                if existing["state"] != ProcessingState.IN_TURN.value:
                    raise LifecycleConflict("turn is not active")
                async with db.execute(
                    f"""SELECT envelope_id FROM turn_run_messages
                        WHERE turn_id = ? AND envelope_id IN ({placeholders})""",
                    (turn_id, *ids),
                ) as cursor:
                    duplicate_members = await cursor.fetchall()
                if duplicate_members:
                    raise LifecycleConflict(
                        "message ID is already a member of this turn"
                    )
                async with db.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) FROM turn_run_messages "
                    "WHERE turn_id = ?",
                    (turn_id,),
                ) as cursor:
                    ordinal_row = await cursor.fetchone()
                first_ordinal = int(ordinal_row[0]) + 1
            else:
                await db.execute(
                    """INSERT INTO turn_runs
                       (turn_id, provider_session_id, state, started_at, completed_at)
                       VALUES (?, ?, ?, ?, NULL)""",
                    (
                        turn_id,
                        provider_session_id,
                        ProcessingState.IN_TURN.value,
                        visible_at,
                    ),
                )
                first_ordinal = 0
            for offset, envelope_id in enumerate(ids):
                await db.execute(
                    "INSERT INTO turn_run_messages(turn_id, envelope_id, ordinal) "
                    "VALUES (?, ?, ?)",
                    (turn_id, envelope_id, first_ordinal + offset),
                )
            cursor = await db.execute(
                f"""UPDATE messages
                    SET processing_state = ?, processing_turn_id = ?, model_visible_at = ?
                    WHERE envelope_id IN ({placeholders}) AND processing_state = ?""",
                (
                    ProcessingState.IN_TURN.value,
                    turn_id,
                    visible_at,
                    *ids,
                    ProcessingState.PENDING.value,
                ),
            )
            if cursor.rowcount != len(ids):
                raise LifecycleConflict("admission changed fewer rows than requested")
            await self._refresh_notice_state(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        run = await self._get_turn_run_unlocked(db, turn_id)
        assert run is not None
        return run

    async def mark_processed(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
        processed_at: int | None = None,
        provider_session_id: str | None = None,
    ) -> TurnRun:
        async with self._inbox_lock:
            return await self._mark_processed_unlocked(
                message_ids,
                turn_id=turn_id,
                processed_at=processed_at,
                provider_session_id=provider_session_id,
            )

    async def _mark_processed_unlocked(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
        processed_at: int | None = None,
        provider_session_id: str | None = None,
    ) -> TurnRun:
        return await self._complete_turn_unlocked(
            self._exact_ids(message_ids),
            (),
            turn_id=turn_id,
            processed_at=processed_at,
            provider_session_id=provider_session_id,
        )

    async def _complete_turn_unlocked(
        self,
        processed: tuple[str, ...],
        renotice: tuple[str, ...],
        *,
        turn_id: str,
        processed_at: int | None = None,
        provider_session_id: str | None = None,
    ) -> TurnRun:
        """One completion transaction for both the plain and renotice paths.

        Every id in ``processed`` becomes PROCESSED; every id in ``renotice``
        returns to PENDING with the one-shot ``renotified`` bit set and its
        notice contributions released, so the same provider session is woken
        again for the redelivery. ``model_visible_at`` survives renotice —
        the row's plaintext already crossed the provider boundary, so it must
        not become a freshness blocker again.
        """
        completed = processed_at if processed_at is not None else _now_ms()
        db = await self._ensure_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            # An optional durable-owner gate: the caller asserts *which*
            # provider session it proved the bodies to, and the same
            # transaction that finalizes the rows checks that claim against
            # ``turn_runs``. An in-memory session assignment alone can no
            # longer acknowledge Inbox work.
            if provider_session_id is not None:
                async with db.execute(
                    "SELECT provider_session_id FROM turn_runs WHERE turn_id = ?",
                    (turn_id,),
                ) as cursor:
                    owner = await cursor.fetchone()
                if owner is None or owner["provider_session_id"] != provider_session_id:
                    raise LifecycleConflict("turn belongs to another provider session")
            expected = await self._turn_message_ids(db, turn_id)
            union = set(processed) | set(renotice)
            if len(expected) != len(union) or set(expected) != union:
                raise LifecycleConflict("completion IDs do not exactly match the turn")
            all_ids = processed + renotice
            in_turn = await _batched_select_ids(
                db,
                "SELECT envelope_id FROM messages WHERE envelope_id IN ({ids}) "
                "AND processing_state = ? AND processing_turn_id = ?",
                all_ids,
                params_after=(ProcessingState.IN_TURN.value, turn_id),
            )
            if len(in_turn) != len(all_ids):
                raise LifecycleConflict("every exact message ID must be in this turn")
            if processed:
                placeholders = ",".join("?" for _ in processed)
                cursor = await db.execute(
                    f"""UPDATE messages SET processing_state = ?, processed_at = ?
                        WHERE envelope_id IN ({placeholders})
                          AND processing_state = ? AND processing_turn_id = ?""",
                    (
                        ProcessingState.PROCESSED.value,
                        completed,
                        *processed,
                        ProcessingState.IN_TURN.value,
                        turn_id,
                    ),
                )
                if cursor.rowcount != len(processed):
                    raise LifecycleConflict(
                        "completion changed fewer rows than requested"
                    )
            if renotice:
                await self._renotice_rows_unlocked(db, renotice, turn_id)
            cursor = await db.execute(
                "UPDATE turn_runs SET state = ?, completed_at = ? "
                "WHERE turn_id = ? AND state = ?",
                (
                    ProcessingState.PROCESSED.value,
                    completed,
                    turn_id,
                    ProcessingState.IN_TURN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LifecycleConflict("turn is not active")
            await self._refresh_notice_state(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        run = await self._get_turn_run_unlocked(db, turn_id)
        assert run is not None
        return run

    async def _renotice_rows_unlocked(
        self,
        db: aiosqlite.Connection,
        renotice: tuple[str, ...],
        turn_id: str,
    ) -> None:
        """Send uncovered rows back to PENDING with the one-shot bit set."""
        placeholders = ",".join("?" for _ in renotice)
        cursor = await db.execute(
            f"""UPDATE messages
                SET processing_state = ?, processing_turn_id = NULL,
                    processed_at = NULL, renotified = 1
                WHERE envelope_id IN ({placeholders})
                  AND processing_state = ? AND processing_turn_id = ?""",
            (
                ProcessingState.PENDING.value,
                *renotice,
                ProcessingState.IN_TURN.value,
                turn_id,
            ),
        )
        if cursor.rowcount != len(renotice):
            raise LifecycleConflict(
                "renotice changed fewer rows than requested"
            )
        # A renoticed row may have been mentioned to the session by an
        # *earlier* notice generation than the one this turn claimed;
        # releasing only the latest delta would leave that contribution in
        # place and filter the row out of every future notice for the
        # session. Release by row, not by notice.
        await db.execute(
            f"DELETE FROM inbox_notice_contributions "
            f"WHERE envelope_id IN ({placeholders})",
            renotice,
        )

    async def requeue_messages(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
    ) -> TurnRun:
        async with self._inbox_lock:
            return await self._requeue_messages_unlocked(message_ids, turn_id=turn_id)

    async def _requeue_messages_unlocked(
        self,
        message_ids: Iterable[str],
        *,
        turn_id: str,
    ) -> TurnRun:
        ids = self._exact_ids(message_ids)
        db = await self._ensure_db()
        placeholders = ",".join("?" for _ in ids)
        await db.execute("BEGIN IMMEDIATE")
        try:
            expected = await self._turn_message_ids(db, turn_id)
            if len(expected) != len(ids) or set(expected) != set(ids):
                raise LifecycleConflict("requeue IDs do not exactly match the turn")
            cursor = await db.execute(
                f"""UPDATE messages
                    SET processing_state = ?, processing_turn_id = NULL,
                        model_visible_at = NULL, processed_at = NULL
                    WHERE envelope_id IN ({placeholders})
                      AND processing_state = ? AND processing_turn_id = ?""",
                (
                    ProcessingState.PENDING.value,
                    *ids,
                    ProcessingState.IN_TURN.value,
                    turn_id,
                ),
            )
            if cursor.rowcount != len(ids):
                raise LifecycleConflict("every exact message ID must be in this turn")
            cursor = await db.execute(
                "UPDATE turn_runs SET state = ?, completed_at = ? "
                "WHERE turn_id = ? AND state = ?",
                ("requeued", _now_ms(), turn_id, ProcessingState.IN_TURN.value),
            )
            if cursor.rowcount != 1:
                raise LifecycleConflict("turn is not active")
            await self._refresh_notice_state(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        run = await self._get_turn_run_unlocked(db, turn_id)
        assert run is not None
        return run

    async def add_message_covers(
        self,
        covered_ids: Iterable[str],
        *,
        source: str,
        by_envelope_id: str | None = None,
        note: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Record disposition declarations; unknown ids are reported, not fatal.

        A cover is a claim about an inbound message, so each id must name a
        locally known row. The declaration itself must never block the send
        or reminder that carries it, which is why unknown ids come back in
        the result instead of raising.
        """
        if source not in ("send", "reminder", "mark"):
            raise ValueError("cover source must be send, reminder, or mark")
        ids = tuple(dict.fromkeys(str(item) for item in covered_ids if item))
        if not ids:
            return {"recorded": [], "unknown": []}
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                known = await _batched_select_ids(
                    db,
                    "SELECT envelope_id FROM messages WHERE envelope_id IN ({ids})",
                    ids,
                )
                recorded = [item for item in ids if item in known]
                unknown = [item for item in ids if item not in known]
                if recorded:
                    now = _now_ms()
                    await db.executemany(
                        "INSERT OR IGNORE INTO message_covers ("
                        "covered_envelope_id, by_envelope_id, source, note, "
                        "turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (item, by_envelope_id, source, note, turn_id, now)
                            for item in recorded
                        ],
                    )
                    # A cover on a row that is sitting in its one-shot
                    # redelivery window is the settlement the redelivery
                    # asked for; without this, an explicitly settled row
                    # would still be re-presented as uncovered.
                    placeholders = ",".join("?" for _ in recorded)
                    await db.execute(
                        f"""UPDATE messages SET processing_state = ?,
                                processed_at = ?, processing_turn_id = NULL
                            WHERE envelope_id IN ({placeholders})
                              AND processing_state = ? AND renotified = 1""",
                        (
                            ProcessingState.PROCESSED.value,
                            now,
                            *recorded,
                            ProcessingState.PENDING.value,
                        ),
                    )
                    await self._refresh_notice_state(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return {"recorded": recorded, "unknown": unknown}

    async def count_uncovered_pending(self) -> int:
        """Count pending rows awaiting their one uncovered redelivery."""
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE processing_state = ? AND renotified = 1",
                (ProcessingState.PENDING.value,),
            ) as cursor:
                row = await cursor.fetchone()
            return int(row["n"])

    async def get_covered_ids(self, message_ids: Iterable[str]) -> set[str]:
        """Return the subset of ``message_ids`` with any cover declaration."""
        ids = tuple(dict.fromkeys(str(item) for item in message_ids if item))
        if not ids:
            return set()
        async with self._inbox_lock:
            db = await self._ensure_db()
            return await _batched_select_ids(
                db,
                "SELECT DISTINCT covered_envelope_id FROM message_covers "
                "WHERE covered_envelope_id IN ({ids})",
                ids,
            )

    async def complete_turn_with_renotice(
        self,
        processed_ids: Iterable[str],
        renotice_ids: Iterable[str],
        *,
        turn_id: str,
        provider_session_id: str | None = None,
    ) -> TurnRun:
        """Finalize a turn while sending uncovered rows back to PENDING.

        The union of both id sets must exactly match the turn membership,
        mirroring ``mark_processed``. Renotice rows return to PENDING with
        ``renotified`` set so the redelivery can happen at most once; the
        turn itself still terminates as PROCESSED.
        """
        processed_tuple = tuple(processed_ids)
        processed = self._exact_ids(processed_tuple) if processed_tuple else ()
        renotice_tuple = tuple(renotice_ids)
        if not renotice_tuple:
            raise ValueError("renotice_ids must be non-empty; use mark_processed")
        renotice = self._exact_ids(renotice_tuple)
        if set(processed) & set(renotice):
            raise LifecycleConflict("processed and renotice ids overlap")
        async with self._inbox_lock:
            return await self._complete_turn_unlocked(
                processed,
                renotice,
                turn_id=turn_id,
                provider_session_id=provider_session_id,
            )

    async def complete_turn_renotice_unrenotified(
        self,
        *,
        turn_id: str,
        provider_session_id: str | None = None,
    ) -> tuple[str, ...]:
        """Fallback completion for when cover reconciliation itself failed.

        Partitions the turn durably instead of trusting the failed read:
        member rows that still hold their one-shot redelivery
        (``renotified = 0``) return to PENDING, rows already redelivered
        once complete as PROCESSED. Coarser than reconciliation — agent and
        system rows get redelivered too — but dup-over-loss with the same
        once-per-row bound, instead of silently settling every row.
        Returns the redelivered ids.
        """
        async with self._inbox_lock:
            db = await self._ensure_db()
            member_ids = await self._turn_message_ids(db, turn_id)
            if not member_ids:
                await self._complete_turn_unlocked(
                    (), (), turn_id=turn_id,
                    provider_session_id=provider_session_id,
                )
                return ()
            unrenotified = await _batched_select_ids(
                db,
                "SELECT envelope_id FROM messages "
                "WHERE envelope_id IN ({ids}) AND renotified = 0",
                member_ids,
            )
            renotice = tuple(i for i in member_ids if i in unrenotified)
            processed = tuple(i for i in member_ids if i not in unrenotified)
            await self._complete_turn_unlocked(
                processed,
                renotice,
                turn_id=turn_id,
                provider_session_id=provider_session_id,
            )
            return renotice

    async def _turn_message_ids(
        self, db: aiosqlite.Connection, turn_id: str
    ) -> tuple[str, ...]:
        async with db.execute(
            "SELECT envelope_id FROM turn_run_messages WHERE turn_id = ? "
            "ORDER BY ordinal",
            (turn_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(row["envelope_id"] for row in rows)

    async def get_turn_run(self, turn_id: str) -> TurnRun | None:
        async with self._inbox_lock:
            db = await self._ensure_db()
            return await self._get_turn_run_unlocked(db, turn_id)

    async def get_active_turn_runs(self) -> tuple[TurnRun, ...]:
        """Return durable Turns that a restarted provider process cannot own."""
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT turn_id FROM turn_runs WHERE state = ? "
                "ORDER BY started_at, turn_id",
                (ProcessingState.IN_TURN.value,),
            ) as cursor:
                rows = await cursor.fetchall()
            active: list[TurnRun] = []
            for row in rows:
                run = await self._get_turn_run_unlocked(db, row["turn_id"])
                if run is not None:
                    active.append(run)
            return tuple(active)

    async def _get_turn_run_unlocked(
        self, db: aiosqlite.Connection, turn_id: str
    ) -> TurnRun | None:
        async with db.execute(
            "SELECT * FROM turn_runs WHERE turn_id = ?", (turn_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        ids = await self._turn_message_ids(db, turn_id)
        return TurnRun(
            row["turn_id"],
            row["provider_session_id"],
            row["state"],
            ids,
            int(row["started_at"]),
            int(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    async def get_in_turn_messages(
        self, turn_id: str, provider_session_id: str | None
    ) -> tuple[StoredMessage, ...]:
        # A stateless provider has no durable identity to authenticate recovery.
        if provider_session_id is None:
            return ()
        async with self._inbox_lock:
            db = await self._ensure_db()
            run = await self._get_turn_run_unlocked(db, turn_id)
            if (
                run is None
                or run.provider_session_id != provider_session_id
                or run.state != ProcessingState.IN_TURN.value
            ):
                return ()
            async with db.execute(
                """SELECT m.* FROM turn_run_messages trm
                   JOIN messages m ON m.envelope_id = trm.envelope_id
                   WHERE trm.turn_id = ? AND m.processing_state = ?
                     AND m.processing_turn_id = ?
                   ORDER BY trm.ordinal""",
                (turn_id, ProcessingState.IN_TURN.value, turn_id),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(self._row_to_msg(row) for row in rows)

    async def get_context_baseline(self, space_id: str, channel_id: str) -> int | None:
        async with self._inbox_lock:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT context_baseline_seq FROM channel_context_state "
                "WHERE space_id = ? AND channel_id = ?",
                (space_id, channel_id),
            ) as cursor:
                row = await cursor.fetchone()
            return int(row[0]) if row is not None else None

    async def set_context_baseline(
        self,
        space_id: str,
        channel_id: str,
        context_baseline_seq: int,
        *,
        updated_at: int | None = None,
    ) -> None:
        if context_baseline_seq < 0:
            raise ValueError("context baseline must be non-negative")
        async with self._inbox_lock:
            db = await self._ensure_db()
            await db.execute(
                """INSERT INTO channel_context_state
                   (space_id, channel_id, context_baseline_seq, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(space_id, channel_id) DO UPDATE SET
                     context_baseline_seq = excluded.context_baseline_seq,
                     updated_at = excluded.updated_at""",
                (
                    space_id,
                    channel_id,
                    context_baseline_seq,
                    updated_at if updated_at is not None else _now_ms(),
                ),
            )
            await db.commit()

    async def get_model_visible_through_seq(
        self,
        turn_id: str,
        space_id: str,
        channel_id: str,
        *,
        candidate_seq: int | None = None,
    ) -> int | None:
        """Return the highest *safe* model-visible channel prefix.

        Server sequence numbers are global, so numeric adjacency is not a
        channel invariant.  A later row is nevertheless not safe to expose
        as a freshness boundary while an earlier locally-known channel row is
        still pending or belongs to another unfinished turn.
        """
        async with self._inbox_lock:
            db = await self._ensure_db()
            # The persisted context baseline is a separate, already trusted
            # history boundary.  It must not be re-proven by (or blocked on)
            # old locally retained Inbox rows.
            async with db.execute(
                "SELECT context_baseline_seq FROM channel_context_state "
                "WHERE space_id = ? AND channel_id = ?",
                (space_id, channel_id),
            ) as cursor:
                baseline_row = await cursor.fetchone()
            baseline = (
                int(baseline_row["context_baseline_seq"])
                if baseline_row is not None
                else None
            )
            async with db.execute(
                """SELECT server_seq, processing_turn_id, processing_state,
                          receipt_disposition, model_visible_at
                   FROM messages
                   WHERE space_id = ? AND channel_id = ?
                     AND envelope_kind != 'dm' AND server_seq IS NOT NULL
                   ORDER BY server_seq ASC""",
                (
                    space_id,
                    channel_id,
                ),
            ) as cursor:
                rows = await cursor.fetchall()
            safe: int | None = None
            for row in rows:
                sequence = int(row["server_seq"])
                if baseline is not None and sequence <= baseline:
                    continue
                # A history candidate proves at most itself.  In particular,
                # a later active-turn row cannot turn that read into an
                # unbounded freshness advance.
                if candidate_seq is not None and sequence > candidate_seq:
                    break
                state = row["processing_state"]
                # A history tool's just-admitted watermark is model-visible
                # and belongs to this turn when it was pending. It is still
                # bounded by every earlier locally-known blocker.
                is_candidate = candidate_seq is not None and sequence == candidate_seq
                is_visible = row["model_visible_at"] is not None or is_candidate
                is_this_turn = row["processing_turn_id"] == turn_id
                terminal = state == ProcessingState.PROCESSED.value
                terminal_receipt = (
                    row["receipt_disposition"] == ReceiptDisposition.TERMINAL.value
                )
                admissible = (
                    terminal_receipt
                    or is_candidate
                    or (
                        is_visible
                        and (
                            terminal
                            or (is_this_turn and state == ProcessingState.IN_TURN.value)
                        )
                    )
                )
                if not admissible:
                    # This is a known same-channel blocker; do not leapfrog it.
                    break
                safe = sequence
            return safe
