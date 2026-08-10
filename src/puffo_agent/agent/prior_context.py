"""Read-only prior-context paging for the MessageStore facade."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .message_store_models import (
    PRIOR_CONTEXT_MAX_BYTES,
    PRIOR_CONTEXT_MAX_ITEMS,
    PriorContextPage,
    ProcessingState,
    ReceiptDisposition,
    StoredMessage,
)


def _bounded_limits(limit: int, max_bytes: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be non-negative")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    return (
        min(limit, PRIOR_CONTEXT_MAX_ITEMS),
        min(max_bytes, PRIOR_CONTEXT_MAX_BYTES),
    )


def _prior_context_route(
    store: Any, anchor: StoredMessage
) -> tuple[list[str], list[Any], tuple[str, ...]] | None:
    order_sql = (
        "COALESCE(m.server_seq, m.after_server_seq, 0)",
        "CASE WHEN m.server_seq IS NOT NULL THEN 0 ELSE 1 END",
        "COALESCE(m.local_ordinal, 0)",
        "m.envelope_id",
    )
    clauses = [
        f"({', '.join(order_sql)}) < (?, ?, ?, ?)",
        "(m.processing_state = ? OR m.receipt_disposition = ?)",
    ]
    params: list[Any] = [
        *store._inbox_order(anchor),
        ProcessingState.PROCESSED.value,
        ReceiptDisposition.TERMINAL.value,
    ]
    if anchor.envelope_kind == "dm":
        peer = anchor.sender_slug or anchor.recipient_slug or ""
        if not peer:
            return None
        clauses.extend(
            ["m.envelope_kind = 'dm'", "(m.sender_slug = ? OR m.recipient_slug = ?)"]
        )
        params.extend((peer, peer))
        return clauses, params, order_sql
    clauses.extend(["m.envelope_kind != 'dm'", "m.space_id = ?", "m.channel_id = ?"])
    params.extend((anchor.space_id, anchor.channel_id))
    is_intro_prompt = (
        anchor.sender_slug == "system"
        and anchor.envelope_id.startswith("intro-prompt-")
        and anchor.thread_root_id == anchor.envelope_id
    )
    if anchor.thread_root_id and not is_intro_prompt:
        clauses.append("(m.envelope_id = ? OR m.thread_root_id = ?)")
        params.extend((anchor.thread_root_id, anchor.thread_root_id))
    else:
        clauses.append("m.thread_root_id IS NULL")
    return clauses, params, order_sql


async def _query_context_rows(
    db: aiosqlite.Connection,
    *,
    clauses: list[str],
    params: list[Any],
    order_sql: tuple[str, ...],
    limit: int,
) -> list[aiosqlite.Row]:
    sql = (
        "SELECT m.* FROM messages m WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY {', '.join(expression + ' DESC' for expression in order_sql)}"
        + " LIMIT ?"
    )
    async with db.execute(sql, tuple([*params, limit + 1])) as cursor:
        return await cursor.fetchall()


def _bounded_page(
    store: Any, rows: list[aiosqlite.Row], *, limit: int, max_bytes: int
) -> PriorContextPage:
    selected_rows: list[aiosqlite.Row] = []
    used_bytes = 0
    has_more = False
    for row in rows:
        if len(selected_rows) >= limit:
            has_more = True
            continue
        body_bytes = len(str(row["content"]).encode("utf-8"))
        if used_bytes + body_bytes > max_bytes:
            has_more = True
            continue
        selected_rows.append(row)
        used_bytes += body_bytes
    return PriorContextPage(
        tuple(store._row_to_msg(row) for row in reversed(selected_rows)),
        has_more,
    )


async def get_prior_context_page(
    store: Any,
    anchor: StoredMessage,
    *,
    limit: int = PRIOR_CONTEXT_MAX_ITEMS,
    max_bytes: int = PRIOR_CONTEXT_MAX_BYTES,
) -> PriorContextPage:
    """Return the same bounded eligible history slice as the former method."""
    if not isinstance(anchor, StoredMessage):
        raise TypeError("anchor must be a StoredMessage")
    limit, max_bytes = _bounded_limits(limit, max_bytes)
    route = _prior_context_route(store, anchor)
    if route is None:
        # An unrouteable anchor has no prior context, not a different return
        # type: callers read ``.items`` / ``.has_more``, and a bare tuple
        # crashes them on the one row shape that reaches here — a DM with
        # neither a sender nor a recipient slug.
        return PriorContextPage((), False)
    clauses, params, order_sql = route
    async with store._inbox_lock:
        rows = await _query_context_rows(
            await store._ensure_db(),
            clauses=clauses,
            params=params,
            order_sql=order_sql,
            limit=limit,
        )
    return _bounded_page(store, rows, limit=limit, max_bytes=max_bytes)
