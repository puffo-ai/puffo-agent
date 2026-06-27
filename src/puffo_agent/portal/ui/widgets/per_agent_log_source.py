"""Tail the agent's per-workspace audit.log (NDJSON written by
``cli_session.AuditLog``) and surface it through the LogView's
snapshot/counter contract."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tail window: 64 KiB / 500 lines is plenty for an operator scrolling
# through recent activity; the full file lives on disk under the
# workspace dir for forensic dives.
_AUDIT_TAIL_BYTES = 64 * 1024
_AUDIT_MAX_LINES = 500
_SUMMARY_CAP = 200


def _format_audit_extras(event: str, extras: dict) -> str:
    """Per-event-type formatter so the row reads as ordinary text
    rather than a JSON blob."""
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
                # Discard the partial leading line.
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
    """Snapshot+counter pair for the LogView. Single source: the
    agent's audit.log tail. Counter is the file's cumulative line
    count (cached incrementally on file size) — monotonic, never
    bumps for activity in other agents."""

    def __init__(self, audit_path: Path) -> None:
        self._audit_path = audit_path
        self._seen_size = 0
        self._line_count = 0

    def snapshot(self) -> list[str]:
        return _read_audit_tail(self._audit_path)

    def counter(self) -> int:
        try:
            size = self._audit_path.stat().st_size
        except OSError:
            return self._line_count
        if size == self._seen_size:
            return self._line_count
        if size < self._seen_size:
            # Rotated / truncated → recount from scratch.
            self._seen_size = 0
            self._line_count = 0
        try:
            with self._audit_path.open("rb") as f:
                f.seek(self._seen_size)
                content = f.read()
        except OSError:
            return self._line_count
        self._line_count += content.count(b"\n")
        self._seen_size = size
        return self._line_count
