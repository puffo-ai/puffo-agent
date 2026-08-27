"""Receipt classification persistence used by the MessageStore facade."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .message_store_models import (
    LifecycleConflict,
    ProcessingState,
    ReceiptDisposition,
    ReceiptResult,
    ReceiptWriteStatus,
)


def _validate_receipt(
    store: Any,
    payload: Any,
    server_seq: int,
    disposition: ReceiptDisposition,
    received_at: int | None,
) -> tuple[ReceiptDisposition, tuple[Any, ...], str]:
    if (
        isinstance(server_seq, bool)
        or not isinstance(server_seq, int)
        or server_seq <= 0
    ):
        raise ValueError("server_seq must be a positive integer")
    normalized = ReceiptDisposition(disposition)
    values = store._payload_values(payload, received_at)
    envelope_id = values[0]
    if not isinstance(envelope_id, str) or not envelope_id:
        raise ValueError("payload must contain a non-empty envelope_id")
    return normalized, values, envelope_id


async def _lookup_existing(
    db: aiosqlite.Connection, envelope_id: str, server_seq: int
) -> tuple[aiosqlite.Row | None, aiosqlite.Row | None]:
    async with db.execute(
        "SELECT server_seq, receipt_disposition, receipt_reason, "
        "processing_state, local_ordinal, after_server_seq "
        "FROM messages WHERE envelope_id = ?",
        (envelope_id,),
    ) as cursor:
        by_id = await cursor.fetchone()
    async with db.execute(
        "SELECT envelope_id FROM messages WHERE server_seq = ?", (server_seq,)
    ) as cursor:
        by_seq = await cursor.fetchone()
    return by_id, by_seq


async def _backfill_missing_sequence(
    store: Any,
    db: aiosqlite.Connection,
    *,
    envelope_id: str,
    server_seq: int,
    disposition: ReceiptDisposition,
    reason: str,
    by_id: aiosqlite.Row,
    by_seq: aiosqlite.Row | None,
) -> ReceiptResult:
    if by_seq is not None and by_seq["envelope_id"] != envelope_id:
        await db.rollback()
        return ReceiptResult(
            ReceiptWriteStatus.CONFLICT,
            disposition,
            "server sequence belongs to another envelope",
            False,
        )
    if (
        by_id["receipt_disposition"] == ReceiptDisposition.LOCAL_RUNTIME.value
        and by_id["processing_state"]
        in {
            ProcessingState.PENDING.value,
            ProcessingState.IN_TURN.value,
            ProcessingState.PROCESSED.value,
        }
        and disposition is ReceiptDisposition.ELIGIBLE
    ):
        cursor = await db.execute(
            """UPDATE messages
               SET server_seq = ?, receipt_disposition = ?, receipt_reason = ?,
                   local_ordinal = NULL, after_server_seq = NULL
               WHERE envelope_id = ? AND server_seq IS NULL
                 AND receipt_disposition = ?""",
            (
                server_seq,
                ReceiptDisposition.ELIGIBLE.value,
                reason,
                envelope_id,
                ReceiptDisposition.LOCAL_RUNTIME.value,
            ),
        )
        if cursor.rowcount != 1:
            raise LifecycleConflict("local receipt promotion lost its association")
        await store._advance_server_sequence_high_water(db, server_seq)
        await db.commit()
        return ReceiptResult(
            ReceiptWriteStatus.COMMITTED, ReceiptDisposition.ELIGIBLE, reason, True
        )
    if (
        by_id["receipt_disposition"] is None
        and by_id["processing_state"] is None
        and disposition is ReceiptDisposition.FOREIGN_DM_GATED
    ):
        await db.execute(
            """UPDATE messages
               SET server_seq = ?, receipt_disposition = ?, receipt_reason = ?
               WHERE envelope_id = ? AND server_seq IS NULL
                 AND receipt_disposition IS NULL AND processing_state IS NULL""",
            (server_seq, disposition.value, reason, envelope_id),
        )
        await store._advance_server_sequence_high_water(db, server_seq)
        await db.commit()
        return ReceiptResult(ReceiptWriteStatus.COMMITTED, disposition, reason, False)
    await db.execute(
        "UPDATE messages SET server_seq = ? WHERE envelope_id = ? AND server_seq IS NULL",
        (server_seq, envelope_id),
    )
    await store._advance_server_sequence_high_water(db, server_seq)
    await db.commit()
    return ReceiptResult(
        ReceiptWriteStatus.COMMITTED,
        disposition,
        "legacy sequence backfilled without Inbox activation",
        store._receipt_ack(disposition),
    )


async def _persist_existing_receipt(
    store: Any,
    db: aiosqlite.Connection,
    *,
    envelope_id: str,
    server_seq: int,
    disposition: ReceiptDisposition,
    reason: str,
    by_id: aiosqlite.Row | None,
    by_seq: aiosqlite.Row | None,
) -> ReceiptResult | None:
    if by_id is None:
        return None
    if by_id["server_seq"] is None:
        return await _backfill_missing_sequence(
            store,
            db,
            envelope_id=envelope_id,
            server_seq=server_seq,
            disposition=disposition,
            reason=reason,
            by_id=by_id,
            by_seq=by_seq,
        )
    if int(by_id["server_seq"]) != server_seq:
        await db.rollback()
        return ReceiptResult(
            ReceiptWriteStatus.CONFLICT,
            disposition,
            "envelope id belongs to another server sequence",
            False,
        )
    stored_raw = by_id["receipt_disposition"]
    stored = ReceiptDisposition(stored_raw) if stored_raw else disposition
    await db.rollback()
    return ReceiptResult(
        ReceiptWriteStatus.IDEMPOTENT,
        stored,
        by_id["receipt_reason"] or reason,
        store._receipt_ack(stored),
    )


async def _insert_new_receipt(
    store: Any,
    db: aiosqlite.Connection,
    *,
    values: tuple[Any, ...],
    server_seq: int,
    disposition: ReceiptDisposition,
    reason: str,
) -> ReceiptResult:
    processing = (
        ProcessingState.PENDING.value
        if disposition is ReceiptDisposition.ELIGIBLE
        else None
    )
    await db.execute(
        """INSERT INTO messages
           (envelope_id, envelope_kind, sender_slug, channel_id, space_id,
            recipient_slug, content_type, content, sent_at, received_at,
            thread_root_id, reply_to_id, thread_root_unverified,
            is_encrypted, server_seq,
            receipt_disposition, receipt_reason, processing_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values + (server_seq, disposition.value, reason, processing),
    )
    await _settle_unverified_thread_claims(db, values)
    await store._advance_server_sequence_high_water(db, server_seq)
    if processing == ProcessingState.PENDING.value:
        await store._refresh_notice_state(db, activated_at=int(values[9]))
    await db.commit()
    return ReceiptResult(
        ReceiptWriteStatus.COMMITTED,
        disposition,
        reason,
        store._receipt_ack(disposition),
    )


async def _settle_unverified_thread_claims(
    db: aiosqlite.Connection,
    values: tuple[Any, ...],
) -> None:
    """Run the deferred root check for replies that claimed this message.

    A reply whose claimed thread root was not locally verifiable at receipt
    time kept the claim with ``thread_root_unverified=1``. When the claimed
    message finally arrives, apply the same ownership rules the live path
    uses: a matching channel/space (or DM conversation) verifies the claim;
    a mismatch (or the "root" being no root at all) wipes it.
    """
    envelope_id = values[0]
    envelope_kind, channel_id, space_id = values[1], values[3], values[4]
    sender_slug, recipient_slug = values[2], values[5]
    own_root, own_root_unverified = values[10], values[12]
    if envelope_kind == "channel" and not own_root:
        await db.execute(
            """UPDATE messages SET thread_root_unverified = 0
               WHERE thread_root_unverified = 1 AND thread_root_id = ?
                 AND channel_id = ?
                 AND (space_id IS NULL OR ? IS NULL OR space_id = ?)""",
            (envelope_id, channel_id, space_id, space_id),
        )
    elif envelope_kind == "channel" and own_root:
        # The claimed "root" is itself a reply: re-root claimants to its
        # own (already validated) root, inheriting its verification state.
        await db.execute(
            """UPDATE messages SET thread_root_id = ?, thread_root_unverified = ?
               WHERE thread_root_unverified = 1 AND thread_root_id = ?
                 AND channel_id = ?""",
            (own_root, 1 if own_root_unverified else 0, envelope_id, channel_id),
        )
    elif envelope_kind == "dm" and not own_root:
        # Same-conversation check mirrors validated_parent: both of the
        # claimant's participants must appear among the root's participants.
        await db.execute(
            """UPDATE messages SET thread_root_unverified = 0
               WHERE thread_root_unverified = 1 AND thread_root_id = ?
                 AND envelope_kind = 'dm'
                 AND sender_slug IN (?, ?) AND recipient_slug IN (?, ?)""",
            (
                envelope_id,
                sender_slug, recipient_slug,
                sender_slug, recipient_slug,
            ),
        )
    elif envelope_kind == "dm" and own_root:
        await db.execute(
            """UPDATE messages SET thread_root_id = ?, thread_root_unverified = ?
               WHERE thread_root_unverified = 1 AND thread_root_id = ?
                 AND envelope_kind = 'dm'
                 AND sender_slug IN (?, ?) AND recipient_slug IN (?, ?)""",
            (
                own_root, 1 if own_root_unverified else 0, envelope_id,
                sender_slug, recipient_slug,
                sender_slug, recipient_slug,
            ),
        )
    # Any remaining claimants live in a different channel/space/DM or
    # claimed a message of another kind: the deferred check failed, wipe.
    await db.execute(
        """UPDATE messages SET thread_root_id = NULL, thread_root_unverified = 0
           WHERE thread_root_unverified = 1 AND thread_root_id = ?""",
        (envelope_id,),
    )


async def store_local_receipt_unlocked(
    store: Any,
    payload: Any,
    *,
    disposition: ReceiptDisposition,
    reason: str,
    received_at: int | None = None,
) -> ReceiptResult:
    """Persist one classified receipt for a delivery with no server sequence.

    Terminal rows allocate a local ordinal so prior context orders them
    against every other sequence-less row.  A gated row deliberately does
    not: it is not part of any ordered view until the operator releases it,
    and ``promote_gated_receipt`` allocates its position then, so an
    approved DM lands where it was approved rather than where it arrived.
    """
    normalized = ReceiptDisposition(disposition)
    if normalized is not ReceiptDisposition.TERMINAL and (
        normalized is not ReceiptDisposition.FOREIGN_DM_GATED
    ):
        raise ValueError(
            "sequence-less receipts carry a terminal or gated disposition; "
            "an admitted one is a local event"
        )
    values = store._payload_values(payload, received_at)
    envelope_id = values[0]
    if not isinstance(envelope_id, str) or not envelope_id:
        raise ValueError("payload must contain a non-empty envelope_id")
    db = await store._ensure_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute(
            "SELECT server_seq, receipt_disposition, receipt_reason "
            "FROM messages WHERE envelope_id = ?",
            (envelope_id,),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            return await _classify_existing_local_row(
                store,
                db,
                existing,
                envelope_id=envelope_id,
                disposition=normalized,
                reason=reason,
            )
        ordinal, frontier = (
            await store._allocate_local_position(db)
            if normalized is ReceiptDisposition.TERMINAL
            else (None, None)
        )
        await db.execute(
            """INSERT INTO messages
               (envelope_id, envelope_kind, sender_slug, channel_id, space_id,
                recipient_slug, content_type, content, sent_at, received_at,
                thread_root_id, reply_to_id, thread_root_unverified,
                is_encrypted, receipt_disposition,
                receipt_reason, local_ordinal, after_server_seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values + (normalized.value, reason, ordinal, frontier),
        )
        await db.commit()
        return ReceiptResult(
            ReceiptWriteStatus.COMMITTED,
            normalized,
            reason,
            store._receipt_ack(normalized),
        )
    except Exception:
        await db.rollback()
        raise


async def _classify_existing_local_row(
    store: Any,
    db: aiosqlite.Connection,
    existing: aiosqlite.Row,
    *,
    envelope_id: str,
    disposition: ReceiptDisposition,
    reason: str,
) -> ReceiptResult:
    """Answer for an envelope the sequence-less lane has already seen."""
    if existing["server_seq"] is not None:
        await db.rollback()
        return ReceiptResult(
            ReceiptWriteStatus.CONFLICT,
            disposition,
            "envelope already owns a server sequence",
            False,
        )
    stored_raw = existing["receipt_disposition"]
    if stored_raw is None:
        # A row written by a plain ``store()`` before this lane classified
        # verdicts. Adopting the verdict is what makes it promotable.
        await db.execute(
            """UPDATE messages SET receipt_disposition = ?, receipt_reason = ?
               WHERE envelope_id = ? AND server_seq IS NULL
                 AND receipt_disposition IS NULL""",
            (disposition.value, reason, envelope_id),
        )
        await db.commit()
        return ReceiptResult(
            ReceiptWriteStatus.COMMITTED,
            disposition,
            reason,
            store._receipt_ack(disposition),
        )
    stored = ReceiptDisposition(stored_raw)
    await db.rollback()
    return ReceiptResult(
        ReceiptWriteStatus.IDEMPOTENT,
        stored,
        existing["receipt_reason"] or reason,
        store._receipt_ack(stored),
    )


async def store_receipt_unlocked(
    store: Any,
    payload: Any,
    *,
    server_seq: int,
    disposition: ReceiptDisposition,
    reason: str,
    received_at: int | None = None,
) -> ReceiptResult:
    """Persist one durably classified receipt without changing facade semantics."""
    disposition, values, envelope_id = _validate_receipt(
        store, payload, server_seq, disposition, received_at
    )
    db = await store._ensure_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        by_id, by_seq = await _lookup_existing(db, envelope_id, server_seq)
        existing = await _persist_existing_receipt(
            store,
            db,
            envelope_id=envelope_id,
            server_seq=server_seq,
            disposition=disposition,
            reason=reason,
            by_id=by_id,
            by_seq=by_seq,
        )
        if existing is not None:
            return existing
        if by_seq is not None:
            await db.rollback()
            return ReceiptResult(
                ReceiptWriteStatus.CONFLICT,
                disposition,
                "server sequence belongs to another envelope",
                False,
            )
        return await _insert_new_receipt(
            store,
            db,
            values=values,
            server_seq=server_seq,
            disposition=disposition,
            reason=reason,
        )
    except Exception:
        await db.rollback()
        raise
