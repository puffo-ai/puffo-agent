"""Bounded reads for per-agent NDJSON audit logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DEFAULT_TAIL = 30
LOG_MAX_TAIL = 2000
LOG_MAX_DELTA_BYTES = 256 * 1024
LOG_TAIL_CHUNK_BYTES = 64 * 1024
LOG_MALFORMED_EVENT = "_raw"


def parse_log_query(
    raw_tail: str | None,
    raw_since: str | None,
) -> tuple[int, int | None]:
    if raw_tail is not None and raw_since is not None:
        raise ValueError("tail and since are mutually exclusive")
    try:
        tail = int(raw_tail) if raw_tail is not None else LOG_DEFAULT_TAIL
    except ValueError:
        tail = LOG_DEFAULT_TAIL
    tail = max(1, min(LOG_MAX_TAIL, tail))
    if raw_since is None:
        return tail, None
    try:
        return tail, max(0, int(raw_since))
    except ValueError:
        return tail, None


def parse_log_line(raw: str) -> dict[str, Any]:
    """Decode one NDJSON line, preserving malformed text as an event."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": LOG_MALFORMED_EVENT,
            "msg": raw[:1024],
        }


def read_tail_bytes(path: Path, file_size: int, target_lines: int) -> bytes:
    """Read a bounded suffix that starts on a complete line."""
    if file_size == 0:
        return b""
    with path.open("rb") as handle:
        newlines_needed = target_lines + 1
        offset = file_size
        buffer = b""
        while offset > 0:
            chunk_size = min(LOG_TAIL_CHUNK_BYTES, offset)
            offset -= chunk_size
            handle.seek(offset)
            buffer = handle.read(chunk_size) + buffer
            if buffer.count(b"\n") >= newlines_needed:
                break
        if buffer.count(b"\n") >= newlines_needed:
            position = len(buffer)
            for _ in range(newlines_needed):
                position = buffer.rfind(b"\n", 0, position)
                if position == -1:
                    break
            if position != -1:
                buffer = buffer[position + 1 :]
        return buffer


def _delta_lines(path: Path, file_size: int, since: int) -> tuple[list[dict], int]:
    offset = since if since <= file_size else 0
    with path.open("rb") as handle:
        handle.seek(offset)
        content = handle.read(LOG_MAX_DELTA_BYTES)
    if len(content) == LOG_MAX_DELTA_BYTES:
        last_newline = content.rfind(b"\n")
        if last_newline > 0:
            content = content[: last_newline + 1]
    lines = [
        parse_log_line(raw)
        for raw in content.decode("utf-8", errors="replace").splitlines()
        if raw.strip()
    ]
    return lines, offset + len(content)


def _tail_lines(path: Path, file_size: int, tail: int) -> list[dict]:
    suffix = read_tail_bytes(path, file_size, tail)
    raw_lines = [
        line
        for line in suffix.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return [parse_log_line(raw) for raw in raw_lines[-tail:]]


def read_audit_log(path: Path, tail: int, since: int | None) -> dict[str, Any]:
    """Return a bounded log window and its next byte cursor."""
    if not path.exists():
        return {
            "lines": [],
            "next_cursor": 0,
            "state": "never_written",
            "note": "audit log not yet created",
        }
    file_size = path.stat().st_size
    if since is None:
        lines = _tail_lines(path, file_size, tail)
        next_cursor = file_size
    else:
        lines, next_cursor = _delta_lines(path, file_size, since)
    result: dict[str, Any] = {"lines": lines, "next_cursor": next_cursor}
    if not lines:
        if since is None:
            result.update(state="empty", note="audit log is empty")
        else:
            result.update(state="up_to_date", note="no new entries since cursor")
    return result


_parse_log_line = parse_log_line
_read_tail_bytes = read_tail_bytes
