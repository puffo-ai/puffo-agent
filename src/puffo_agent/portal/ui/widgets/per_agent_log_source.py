"""Combine the daemon's Python-logger ring (filtered to a single
agent) with the agent's per-workspace audit.log (NDJSON written by
``cli_session.AuditLog``). Surfaces both streams in the same Logs
tab — Python logging captures spawn / WS / supervisor events, audit
captures the assistant's text + tool calls per turn."""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Bound the audit-log tail we read per poll; the file is unbounded and
# rotation isn't implemented yet (cli_session.AuditLog.write appends).
_AUDIT_TAIL_BYTES = 64 * 1024
_AUDIT_MAX_LINES = 500
_SUMMARY_CAP = 200


def _format_audit_extras(event: str, extras: dict) -> str:
    """One-line summary of an audit row's non-meta fields, shaped per
    the most common event types so the Logs tab stays readable."""
    if event == "assistant.text":
        text = extras.get("text", "")
        if isinstance(text, str):
            return text[:_SUMMARY_CAP] + ("…" if len(text) > _SUMMARY_CAP else "")
    if event == "tool":
        name = extras.get("name", "")
        inp = extras.get("input", {})
        inp_str = json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp)
        if len(inp_str) > _SUMMARY_CAP:
            inp_str = inp_str[:_SUMMARY_CAP] + "…"
        return f"{name} {inp_str}".strip()
    if event in ("turn.input", "turn.end", "session.start"):
        flat = " ".join(
            f"{k}={v}" for k, v in extras.items()
            if not isinstance(v, (dict, list))
        )
        return flat[:_SUMMARY_CAP] + ("…" if len(flat) > _SUMMARY_CAP else "")
    rendered = json.dumps(extras, ensure_ascii=False)
    return rendered[:_SUMMARY_CAP] + ("…" if len(rendered) > _SUMMARY_CAP else "")


def _read_audit_tail(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    try:
        with path.open("rb") as f:
            if size > _AUDIT_TAIL_BYTES:
                f.seek(size - _AUDIT_TAIL_BYTES)
                # Discard the partial leading line; sizes >cap mean we
                # almost certainly landed mid-record.
                f.readline()
            content = f.read()
    except OSError:
        return []
    text = content.decode("utf-8", errors="replace")
    out: list[str] = []
    for raw in text.splitlines()[-_AUDIT_MAX_LINES:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        ts = str(d.get("ts", ""))
        ev = str(d.get("event", "?"))
        extras = {k: v for k, v in d.items() if k not in ("ts", "agent", "event")}
        out.append(f"{ts} [audit/{ev}] {_format_audit_extras(ev, extras)}")
    return out


class PerAgentLogSource:
    """Snapshot+counter pair the LogView can poll. Merges the
    daemon-wide Python logger ring (filtered to lines mentioning this
    agent's id or slug) with the agent's audit.log tail.

    Dedup-by-content: tracks every distinct line ever emitted so the
    LogView delta-slice contract can't double-render. Naive
    ``py_counter + audit_line_count`` would bump the counter every
    time ANOTHER agent writes a Python log line — none of those
    deltas land in this snapshot, so the slice picks up the END of
    the already-displayed merged view and re-appends duplicates."""

    # Cap on the unique-line history we keep in memory. Bigger than the
    # LogView's 500-line display so the user can scroll back through a
    # ring that exceeds one screen.
    _EMITTED_MAX = 1000

    def __init__(
        self,
        agent_id: str,
        slug: str,
        audit_path: Path,
        py_snapshot: Callable[[], list[str]],
        py_counter: Callable[[], int],
    ) -> None:
        self._tokens = {t for t in (agent_id, slug) if t}
        self._audit_path = audit_path
        self._py_snapshot = py_snapshot
        self._py_counter = py_counter
        self._emitted: deque[str] = deque(maxlen=self._EMITTED_MAX)
        self._emitted_set: set[str] = set()
        self._emit_count = 0

    def snapshot(self) -> list[str]:
        return list(self._emitted)

    def counter(self) -> int:
        self._refresh()
        return self._emit_count

    def _refresh(self) -> None:
        py = [
            ln for ln in self._py_snapshot()
            if any(t in ln for t in self._tokens)
        ]
        audit = _read_audit_tail(self._audit_path)
        # Lex sort on the leading timestamp is good enough — Python
        # ``YYYY-MM-DD HH:MM:SS,ms`` and audit ``YYYY-MM-DDTHH:MM:SS+TZ``
        # both sort chronologically within the same day (' ' < 'T');
        # cross-day comparisons are exact.
        merged = sorted(py + audit, key=lambda ln: ln[:23])
        for ln in merged:
            if ln in self._emitted_set:
                continue
            if (
                self._emitted.maxlen is not None
                and len(self._emitted) == self._emitted.maxlen
            ):
                # Evict from the dedup set in lockstep with the deque.
                self._emitted_set.discard(self._emitted[0])
            self._emitted.append(ln)
            self._emitted_set.add(ln)
            self._emit_count += 1
