"""Combine the daemon's Python-logger ring (filtered to a single
agent) with the agent's per-workspace audit.log (NDJSON written by
``cli_session.AuditLog``). Surfaces both streams in the same Logs
tab — Python logging captures spawn / WS / supervisor events, audit
captures the assistant's text + tool calls per turn."""

from __future__ import annotations

import json
import logging
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
    agent's id or slug) with the agent's audit.log tail."""

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
        # Incremental line-count cache: previous file size → cumulative
        # newline count. Polled every tick; recounting from scratch a
        # multi-MB audit.log would burn cycles, so we only walk the
        # bytes added since the last poll.
        self._audit_seen_size = 0
        self._audit_line_count = 0

    def snapshot(self) -> list[str]:
        py = [
            ln for ln in self._py_snapshot()
            if any(t in ln for t in self._tokens)
        ]
        audit = _read_audit_tail(self._audit_path)
        # Lexicographic sort on the leading timestamp is good enough —
        # Python ``YYYY-MM-DD HH:MM:SS,ms`` and audit ``YYYY-MM-DDTHH:MM:SS+TZ``
        # both sort chronologically within the same day even though the
        # separator differs (' ' < 'T'); cross-day comparisons are
        # exact.
        return sorted(py + audit, key=lambda ln: ln[:23])

    def counter(self) -> int:
        # Counter must be in LINE units to match the LogView delta-slice
        # contract; mixing byte sizes with line counts double-renders
        # already-shown audit rows every time the file grows.
        return self._py_counter() + self._audit_line_count_cached()

    def _audit_line_count_cached(self) -> int:
        try:
            size = self._audit_path.stat().st_size
        except OSError:
            return self._audit_line_count
        if size == self._audit_seen_size:
            return self._audit_line_count
        if size < self._audit_seen_size:
            # Rotated / truncated → recount from scratch.
            self._audit_seen_size = 0
            self._audit_line_count = 0
        try:
            with self._audit_path.open("rb") as f:
                f.seek(self._audit_seen_size)
                content = f.read()
        except OSError:
            return self._audit_line_count
        self._audit_line_count += content.count(b"\n")
        self._audit_seen_size = size
        return self._audit_line_count
