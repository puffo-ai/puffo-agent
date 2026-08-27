"""History-window implementation for the agent-facing ``read_history`` tool."""

from __future__ import annotations

import base64
import json
from typing import Any

from ..agent.message_projection import (
    format_history_read_result,
    format_message_group,
)
from ..agent.message_store_models import parse_inbox_target
from .data_client import DataNotFound
from .puffo_core_tools import _stage_model_visible_messages


def _encode_history_cursor(*, target: str, direction: str, anchor: str) -> str:
    value = {"v": 1, "target": target, "direction": direction, "anchor": anchor}
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str) -> dict[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "invalid history cursor; retry read_history without cursor"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or not isinstance(value.get("target"), str)
        or value.get("direction") not in {"older", "newer"}
        or not isinstance(value.get("anchor"), str)
        or not value["target"]
        or not value["anchor"]
    ):
        raise RuntimeError(
            "invalid history cursor; retry read_history without cursor"
        )
    return {
        "target": value["target"],
        "direction": value["direction"],
        "anchor": value["anchor"],
    }


def _resolve_history_window(
    *,
    target: str,
    cursor: str,
    before_message_id: str,
    after_message_id: str,
) -> tuple[str, str, str]:
    if cursor:
        if before_message_id or after_message_id:
            raise RuntimeError(
                "cursor cannot be combined with before_message_id or "
                "after_message_id"
            )
        decoded = _decode_history_cursor(cursor)
        if target and target != decoded["target"]:
            raise RuntimeError("history cursor belongs to another target")
        return decoded["target"], decoded["direction"], decoded["anchor"]
    if not target:
        raise RuntimeError("target is required when cursor is omitted")
    if before_message_id and after_message_id:
        raise RuntimeError(
            "before_message_id and after_message_id are mutually exclusive"
        )
    if before_message_id:
        return target, "older", before_message_id
    if after_message_id:
        return target, "newer", after_message_id
    return target, "older", ""


def history_tool_arguments(
    *,
    target: str,
    cursor: str,
    before_message_id: str,
    after_message_id: str,
    limit: int,
) -> dict[str, object]:
    arguments: dict[str, object] = {}
    for key, value in (
        ("target", target),
        ("cursor", cursor),
        ("before_message_id", before_message_id),
        ("after_message_id", after_message_id),
    ):
        if value:
            arguments[key] = value
    if limit != 50:
        arguments["limit"] = limit
    return arguments


def _bounded_window(
    rows: list[Any],
    *,
    limit: int,
    direction: str,
    anchored: bool,
) -> tuple[list[Any], bool, bool]:
    has_more = len(rows) > limit
    if direction == "newer":
        return rows[:limit], anchored, has_more
    return rows[-limit:], has_more, anchored


def _history_cursors(
    messages: list[Any],
    *,
    target: str,
    has_older: bool,
    has_newer: bool,
    anchor: str,
) -> tuple[str, str]:
    oldest_anchor = str(messages[0].envelope_id) if messages else anchor
    newest_anchor = str(messages[-1].envelope_id) if messages else anchor
    older = (
        _encode_history_cursor(
            target=target,
            direction="older",
            anchor=oldest_anchor,
        )
        if has_older and oldest_anchor
        else ""
    )
    newer = (
        _encode_history_cursor(
            target=target,
            direction="newer",
            anchor=newest_anchor,
        )
        if has_newer and newest_anchor
        else ""
    )
    return older, newer


def _anchor_matches_target(
    message: Any,
    *,
    kind: str,
    space_id: str,
    channel_id: str,
    tail: str,
) -> bool:
    if kind == "dm":
        peer = tail.lstrip("@")
        return (
            message.envelope_kind == "dm"
            and peer in {message.sender_slug, message.recipient_slug}
        )
    if (
        message.envelope_kind != "channel"
        or message.space_id != space_id
        or message.channel_id != channel_id
    ):
        return False
    return not tail or message.envelope_id == tail or message.thread_root_id == tail


async def _validate_anchor(
    cfg: Any,
    *,
    anchor: str,
    kind: str,
    space_id: str,
    channel_id: str,
    tail: str,
) -> None:
    if not anchor:
        return
    message = await cfg.data_client.get_message_by_envelope(anchor)
    if message is None or not _anchor_matches_target(
        message,
        kind=kind,
        space_id=space_id,
        channel_id=channel_id,
        tail=tail,
    ):
        raise RuntimeError("history boundary message does not belong to target")


async def _read_rows(
    cfg: Any,
    *,
    kind: str,
    channel_id: str,
    tail: str,
    anchor: str,
    direction: str,
    limit: int,
) -> tuple[list[Any], dict[str, int]]:
    before = anchor if anchor and direction == "older" else None
    after = anchor if anchor and direction == "newer" else None
    if kind == "dm":
        messages = await cfg.data_client.get_dm_history(
            tail.lstrip("@"),
            limit=limit + 1,
            before_envelope_id=before,
            after_envelope_id=after,
        )
        return messages, {}
    if tail:
        messages = await cfg.data_client.get_thread_messages(
            tail,
            limit=limit + 1,
            since_envelope_id=after,
            before_envelope_id=before,
        )
        return messages, {}
    roots = await cfg.data_client.get_channel_roots(
        channel_id,
        limit=limit + 1,
        since_envelope_id=after,
        before_envelope_id=before,
    )
    return (
        [entry.message for entry in roots],
        {entry.message.envelope_id: entry.reply_count for entry in roots},
    )


async def read_history_messages(
    cfg: Any,
    *,
    target: str,
    cursor: str,
    before_message_id: str,
    after_message_id: str,
    limit: int,
    tool_arguments: dict[str, object],
) -> str:
    """Read one bounded local-history window for a canonical target."""
    target, direction, anchor = _resolve_history_window(
        target=target,
        cursor=cursor,
        before_message_id=before_message_id,
        after_message_id=after_message_id,
    )
    try:
        kind, space_id, channel_id, tail = parse_inbox_target(target)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    if isinstance(limit, bool):
        raise RuntimeError("limit must be an integer")
    limit = max(1, min(int(limit), 200))
    await _validate_anchor(
        cfg,
        anchor=anchor,
        kind=kind,
        space_id=space_id,
        channel_id=channel_id,
        tail=tail,
    )
    try:
        messages, reply_counts = await _read_rows(
            cfg,
            kind=kind,
            channel_id=channel_id,
            tail=tail,
            anchor=anchor,
            direction=direction,
            limit=limit,
        )
    except DataNotFound:
        label = "thread" if tail and kind != "dm" else kind
        raise RuntimeError(f"no such {label} target: {target}") from None
    messages, has_older, has_newer = _bounded_window(
        messages,
        limit=limit,
        direction=direction,
        anchored=bool(anchor),
    )
    marker = await _stage_model_visible_messages(
        cfg,
        messages,
        tool_name="read_history",
        tool_arguments=tool_arguments,
    )
    body = format_message_group(
        messages,
        current_agent_aliases=(cfg.slug,),
        thread_root_id=tail if kind != "dm" and tail else "",
        reply_counts=reply_counts or None,
    )
    older_cursor, newer_cursor = _history_cursors(
        messages,
        target=target,
        has_older=has_older,
        has_newer=has_newer,
        anchor=anchor,
    )
    result = format_history_read_result(
        messages,
        body=body,
        has_older=has_older,
        has_newer=has_newer,
        older_cursor=older_cursor,
        newer_cursor=newer_cursor,
        target_ref=target,
    )
    return f"{result}\n{marker}" if marker else result
