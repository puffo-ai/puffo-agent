"""Per-agent logger that prefixes each record with ``agent <id>:``
so log lines from a multi-agent daemon stay attributable."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any


RUNTIME_EVENT_FIELDS = frozenset({
    "agent_id", "agent_slug", "turn_id", "batch_id",
    "provider_session_id", "provider_turn_id",
    "tool_call_id", "correlation_key", "envelope_id", "space_id",
    "channel_id", "thread_root_id", "dm_peer",
    "context_baseline_seq", "seen_seq", "latest_seq",
    "latest_seq_before_send", "seq",
    "mode", "attempt_phase", "transport", "attempt", "state",
    "requires_encryption",
    "duration_ms", "message_count", "changed_message_count",
    "total_pending_messages",
    "formatted_bytes", "projected_tokens", "used_tokens_before",
    "used_tokens_after", "context_window", "decision_reason", "error_category",
    "target_count", "envelope_count", "first_seq", "last_seq",
    "envelope_ids", "routes",
    "target", "server_seq", "message_id", "notice_generation",
    "send_attempt_id", "outcome", "remaining_count", "snapshot_generation",
    "runtime_ref", "session_ref", "turn_ref", "permission_ref", "run_id",
    "event_id", "event_type", "outbox_sequence", "retry_count",
    "capability", "capability_decision", "error_code", "error_type",
    "first_sequence", "last_sequence", "event_count",
})
RUNTIME_EVENT_NAMES = frozenset({
    "inbox.received",
    "inbox.receipt_committed",
    "inbox.persisted",
    "notice.armed",
    "notice.due",
    "notice.deferred",
    "notice.admitted",
    "inbox.read_staged",
    "inbox.read_admitted",
    "inbox.row_in_turn",
    "inbox.row_processed",
    "inbox.row_requeued",
    "inbox.row_quarantined",
    "batch.planned",
    "turn.admitted",
    "history.read_staged",
    "history.read_admitted",
    "send.attempted",
    "send.held",
    "held.admission_failed",
    "held.synchronized",
    "reconsideration.eligible",
    "reconsideration.blocked",
    "send.committed",
    "send.failed",
    "context.checked",
    "context.compacted",
    "context.rollover",
    "turn.processed",
    "turn.requeued",
    "turn.failed",
    "turn.finalized",
    "turn.autonomous_adoption",
    "turn.autonomous_finalized",
    "turn.uncovered_messages",
    "turn.cover_reconciliation_failed",
    "runtime.command",
    "runtime.normalized_event",
    "runtime.projected",
    "runtime.enqueued",
    "runtime.batch_attempt",
    "runtime.acknowledged",
    "runtime.retry",
    "runtime.discarded",
    "runtime.capacity",
    "runtime.recovery",
    "processing_report.acknowledged",
    "processing_report.retry",
    "processing_report.isolated",
    "processing_report.discarded",
})
_OBSERVABILITY_WARNED: set[str] = set()
_MAX_EVENT_ENVELOPE_IDS = 16
_MAX_EVENT_ROUTES = 16
_ROUTE_FIELDS = frozenset({
    "space_id", "channel_id", "thread_root_id", "dm_peer",
    "count", "min_seq", "max_seq",
})

_TOKENISH = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:access|refresh|id)[_-]?token\s*[:=]\s*\S+|"
    r"sk-[a-z0-9_-]{12,}|eyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?)"
)


def safe_value_summary(value: Any) -> dict[str, Any]:
    """Return a content-free, bounded description suitable for audit logs."""
    summary: dict[str, Any] = {"type": type(value).__name__}
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        summary.update({"length": len(value), "bytes": len(encoded)})
    elif isinstance(value, (bytes, bytearray)):
        summary["bytes"] = len(value)
    elif isinstance(value, Mapping):
        keys = sorted(str(key) for key in value.keys())
        summary.update({"field_count": len(keys), "fields": keys[:64]})
    elif isinstance(value, Sequence):
        summary["item_count"] = len(value)
    return summary


def diagnostic_category(value: Any) -> str:
    """Classify diagnostics without returning their potentially sensitive text."""
    text = str(value or "").lower()
    if not text:
        return "empty"
    if (
        "auth" in text
        or "token" in text
        or "bearer" in text
        or "401" in text
        or "403" in text
    ):
        return "authentication"
    if ("rate" in text and "limit" in text) or "429" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "json" in text or "parse" in text or "decode" in text:
        return "protocol_parse"
    if "connect" in text or "socket" in text or "network" in text:
        return "transport"
    if "permission" in text or "denied" in text:
        return "permission"
    return "provider_error"


def safe_diagnostic_summary(value: Any) -> str:
    """Compact whitelist-only description of provider output."""
    raw = str(value or "")
    category = diagnostic_category(raw)
    text = _TOKENISH.sub("[REDACTED]", raw)
    encoded = text.encode("utf-8", errors="replace")
    return f"category={category} chars={len(text)} bytes={len(encoded)}"


def _warn_observability_once(category: str) -> None:
    """Report a logging-contract fault without exposing rejected values."""
    try:
        if category in _OBSERVABILITY_WARNED:
            return
        _OBSERVABILITY_WARNED.add(category)
        logging.getLogger(__name__).warning(
            "runtime observability degraded category=%s", category
        )
    except BaseException:
        return


def log_runtime_event(
    logger_target: logging.Logger | logging.LoggerAdapter,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    try:
        supported_event = (
            isinstance(event, str) and event in RUNTIME_EVENT_NAMES
        )
    except BaseException:
        supported_event = False
    if not supported_event:
        _warn_observability_once("unsupported_event")
        return

    record: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key not in RUNTIME_EVENT_FIELDS:
            _warn_observability_once("unsupported_field")
            continue
        try:
            if value is None or value == "":
                continue
            if key == "envelope_ids":
                if (
                    not isinstance(value, (list, tuple))
                    or len(value) > _MAX_EVENT_ENVELOPE_IDS
                    or not all(
                        isinstance(item, str) and item
                        for item in value
                    )
                ):
                    _warn_observability_once("unsupported_value")
                    continue
                record[key] = list(value)
                continue
            if key == "routes":
                if not isinstance(value, (list, tuple)) or len(value) > _MAX_EVENT_ROUTES:
                    _warn_observability_once("unsupported_value")
                    continue
                safe_routes: list[dict[str, Any]] = []
                valid_routes = True
                for route in value:
                    if not isinstance(route, Mapping) or not set(route) <= _ROUTE_FIELDS:
                        valid_routes = False
                        break
                    safe_route = {
                        route_key: route_value
                        for route_key, route_value in route.items()
                        if route_value not in (None, "")
                        and isinstance(route_value, (str, int))
                        and not isinstance(route_value, bool)
                    }
                    if len(safe_route) != sum(
                        route_value not in (None, "")
                        for route_value in route.values()
                    ):
                        valid_routes = False
                        break
                    safe_routes.append(safe_route)
                if not valid_routes:
                    _warn_observability_once("unsupported_value")
                    continue
                record[key] = safe_routes
                continue
            if not isinstance(value, (str, int, float, bool)):
                _warn_observability_once("unsupported_value")
                continue
            if isinstance(value, str):
                value = _TOKENISH.sub("[REDACTED]", value)[:256]
            json.dumps(value, ensure_ascii=True, allow_nan=False)
            record[key] = value
        except BaseException:
            _warn_observability_once("unsupported_value")
            continue

    try:
        encoded = json.dumps(
            record,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        logger_target.log(level, "runtime_event=%s", encoded)
    except BaseException:
        _warn_observability_once("event_emission_failed")
        return


class _AgentLogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        agent_id = self.extra.get("agent_id") if self.extra else ""
        if agent_id:
            return f"agent {agent_id}: {msg}", kwargs
        return msg, kwargs


def agent_logger(name: str, agent_id: str) -> logging.LoggerAdapter:
    return _AgentLogAdapter(logging.getLogger(name), {"agent_id": agent_id})
