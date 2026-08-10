"""Puffo Runtime Events v1 schema, validation, and safe projection."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .harness.driver import HarnessEvent, HarnessEventType


RUNTIME_EVENT_TYPES = frozenset({
    "turn.started",
    "activity.updated",
    "output.updated",
    "tool.updated",
    "permission.updated",
    "turn.finished",
})
OUTPUT_KINDS = frozenset({"progress", "result"})
OUTPUT_PHASES = frozenset({"start", "delta", "end"})
TOOL_STATES = frozenset({"running", "succeeded", "failed"})
PERMISSION_STATES = frozenset({"pending", "approved", "denied", "expired"})
TURN_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "abandoned"})
_SAFE_ERROR_CODES = frozenset({
    "provider_unavailable", "runtime_exited", "protocol_error",
    "permission_denied", "cancel_failed", "unknown",
})
_SAFE_MESSAGES = {
    "provider_unavailable": "The Agent runtime became unavailable.",
    "runtime_exited": "The Agent runtime stopped before completing the turn.",
    "protocol_error": "The Agent runtime returned an invalid response.",
    "permission_denied": "The requested operation was not permitted.",
    "cancel_failed": "The Agent runtime could not cancel the turn.",
    "unknown": "The Agent runtime could not complete the turn.",
}
_SAFE_ACTIVITY = re.compile(r"^[\w .,:'!?()/+\-]{1,160}$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class TrustedScope:
    """Runtime Event v1's fixed operator audience."""

    kind: str = "operator"
    context_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind != "operator" or self.context_ref is not None:
            raise ValueError("Runtime Event v1 scope must be operator")

    @classmethod
    def from_runtime_context(
        cls, context_refs: tuple[str, ...] | list[str] | None
    ) -> TrustedScope:
        return cls()

    def as_dict(self) -> dict[str, str]:
        return {"kind": "operator"}


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    agent_id: str
    session_ref: str
    turn_ref: str
    type: str
    payload: Mapping[str, Any]
    scope: TrustedScope = field(default_factory=TrustedScope)
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    version: int = 1

    def __post_init__(self) -> None:
        validate_runtime_event(self)
        object.__setattr__(self, "payload", _freeze_json(self.payload))

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "session_ref": self.session_ref,
            "turn_ref": self.turn_ref,
            "scope": self.scope.as_dict(),
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": _thaw_json(self.payload),
        }


def validate_runtime_event(event: RuntimeEvent) -> None:
    if event.version != 1:
        raise ValueError("runtime event version must be 1")
    if event.type not in RUNTIME_EVENT_TYPES:
        raise ValueError(f"unsupported runtime event type: {event.type}")
    if not all((event.event_id, event.agent_id, event.session_ref, event.turn_ref)):
        raise ValueError("runtime event references must be non-empty")
    payload = event.payload
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    if event.type == "turn.started" and payload:
        raise ValueError("turn.started payload must be empty")
    if event.type == "activity.updated":
        if set(payload) != {"text"}:
            raise ValueError("activity.updated requires only text")
        text = payload["text"]
        if text is not None and (
            not isinstance(text, str) or not _SAFE_ACTIVITY.fullmatch(text)
        ):
            raise ValueError("activity text is not user-safe")
    if event.type == "output.updated":
        required = {"block_id", "kind", "phase"}
        if not required <= set(payload):
            raise ValueError("output.updated missing required fields")
        allowed = required | ({"delta"} if payload["phase"] == "delta" else set())
        if set(payload) != allowed:
            raise ValueError("output.updated has unsupported fields")
        if payload["kind"] not in OUTPUT_KINDS:
            raise ValueError("invalid output kind")
        if payload["phase"] not in OUTPUT_PHASES:
            raise ValueError("invalid output phase")
        delta = payload.get("delta")
        if payload["phase"] == "delta":
            if not isinstance(delta, str) or not delta:
                raise ValueError("output delta must be non-empty")
        elif "delta" in payload:
            raise ValueError("delta is valid only for delta phase")
    if event.type == "tool.updated":
        if set(payload) != {"tool_call_ref", "label", "state"}:
            raise ValueError("tool.updated missing required fields")
        if payload["state"] not in TOOL_STATES:
            raise ValueError("invalid tool state")
    if event.type == "permission.updated":
        if set(payload) != {"permission_ref", "state", "title"}:
            raise ValueError("permission.updated missing required fields")
        if payload["state"] not in PERMISSION_STATES:
            raise ValueError("invalid permission state")
    if event.type == "turn.finished":
        if not set(payload) <= {"outcome", "error"}:
            raise ValueError("turn.finished has unsupported fields")
        if payload.get("outcome") not in TURN_OUTCOMES:
            raise ValueError("invalid turn outcome")
        if payload.get("outcome") != "failed" and "error" in payload:
            raise ValueError("turn.finished error requires failed outcome")
        if payload.get("outcome") == "failed" and "error" in payload:
            error = payload["error"]
            if (
                not isinstance(error, Mapping)
                or set(error) != {"code", "message", "retryable"}
            ):
                raise ValueError("turn.finished error is invalid")
            code = error["code"]
            if code not in _SAFE_ERROR_CODES:
                raise ValueError("turn.finished error code is invalid")
            if error["message"] != _SAFE_MESSAGES[code]:
                raise ValueError("turn.finished error message is invalid")
            if type(error["retryable"]) is not bool:
                raise ValueError("turn.finished error retryable is invalid")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class LifecycleValidator:
    """Stateful validator; call before persisting each event."""

    # A worker's validator lives as long as the daemon, so finished-turn
    # memory has to be bounded.  The cost of eviction is that a duplicate
    # `turn.started` for a turn older than this many turns is re-admitted
    # instead of rejected as terminal; every other late event for it still
    # fails the "event does not belong to active turn" check.
    _FINISHED_CAPACITY = 1024

    def __init__(self) -> None:
        self._active: str | None = None
        # Ordered set: keys are finished turn refs, in eviction order.
        self._finished: dict[str, None] = {}
        self._blocks: dict[tuple[str, str], str] = {}

    @property
    def active_turn_ref(self) -> str | None:
        return self._active

    def accept(self, event: RuntimeEvent) -> None:
        turn = event.turn_ref
        if turn in self._finished:
            raise ValueError("turn is already terminal")
        if event.type == "turn.started":
            if self._active is not None:
                raise ValueError("another turn is active")
            self._active = turn
            return
        if self._active != turn:
            raise ValueError("event does not belong to active turn")
        if event.type == "output.updated":
            key = (turn, str(event.payload["block_id"]))
            phase = str(event.payload["phase"])
            previous = self._blocks.get(key)
            if phase == "start" and previous is None:
                self._blocks[key] = "start"
            elif phase == "delta" and previous in {"start", "delta"}:
                self._blocks[key] = "delta"
            elif phase == "end" and previous in {"start", "delta"}:
                self._blocks[key] = "end"
            else:
                raise ValueError("invalid output block transition")
        if event.type == "turn.finished":
            if any(
                key[0] == turn and phase != "end"
                for key, phase in self._blocks.items()
            ):
                raise ValueError("turn cannot finish with an open output block")
            # The turn is terminal, so its per-block rows can never be
            # consulted again; dropping them also keeps the scan above O(open
            # blocks) instead of O(blocks ever seen by this process).
            for key in [key for key in self._blocks if key[0] == turn]:
                del self._blocks[key]
            self._finished[turn] = None
            while len(self._finished) > self._FINISHED_CAPACITY:
                self._finished.pop(next(iter(self._finished)))
            self._active = None


def safe_error(
    code: str, *, retryable: bool = False
) -> dict[str, Any]:
    normalized = code if code in _SAFE_ERROR_CODES else "unknown"
    return {
        "code": normalized,
        "message": _SAFE_MESSAGES[normalized],
        "retryable": bool(retryable),
    }


class RuntimeEventProjector:
    """Allowlist-only projection from normalized Driver events."""

    def __init__(
        self,
        *,
        agent_id: str,
        session_ref: str,
        scope: TrustedScope | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self.agent_id = agent_id
        self.session_ref = session_ref
        self.scope = scope or TrustedScope()
        self.id_factory = id_factory or (lambda: f"evt_{uuid.uuid4().hex}")
        self._block_refs: dict[tuple[str, str], str] = {}
        self._tool_refs: dict[tuple[str, str], str] = {}
        self._started_blocks: set[tuple[str, str]] = set()

    def _make(
        self, event: HarnessEvent, type_: str, payload: Mapping[str, Any]
    ) -> RuntimeEvent | None:
        if event.turn_ref is None:
            return None
        return RuntimeEvent(
            agent_id=self.agent_id,
            session_ref=self.session_ref,
            turn_ref=str(event.turn_ref),
            type=type_,
            payload=payload,
            scope=self.scope,
            event_id=self.id_factory(),
            occurred_at=event.occurred_at,
        )

    def project(self, event: HarnessEvent) -> RuntimeEvent | None:
        kind = str(
            event.type.value if isinstance(event.type, HarnessEventType)
            else event.type
        )
        data = event.data
        if kind == "turn.started":
            return self._make(event, "turn.started", {})
        if kind == "turn.assistant_delta":
            text = data.get("text")
            if not isinstance(text, str) or not text:
                return None
            return self._make(event, "output.updated", {
                "block_id": self._block_ref(event),
                "kind": (
                    str(data.get("kind"))
                    if data.get("kind") in OUTPUT_KINDS
                    else "result"
                ),
                "phase": "delta",
                "delta": text,
            })
        if kind == "turn.assistant_completed":
            # Empty native assistant blocks are suppressed wholesale; emitting
            # an end without a prior delta is invalid lifecycle output.
            if (str(event.turn_ref), str(data.get("block_id") or "result")) not in self._block_refs:
                return None
            return self._make(event, "output.updated", {
                "block_id": self._block_ref(event),
                "kind": (
                    str(data.get("kind"))
                    if data.get("kind") in OUTPUT_KINDS
                    else "result"
                ),
                "phase": "end",
            })
        if kind == "turn.tool_started":
            return self._tool(event, "running")
        if kind == "turn.tool_completed":
            outcome = data.get("outcome", "succeeded")
            if outcome not in {"succeeded", "failed"}:
                return None
            return self._tool(event, str(outcome))
        if kind in {"turn.permission_requested", "turn.permission_updated"}:
            state = str(data.get("state") or "pending")
            if state not in PERMISSION_STATES:
                return None
            return self._make(event, "permission.updated", {
                "permission_ref": str(data.get("permission_ref") or ""),
                "state": state,
                "title": "Permission required",
            })
        if kind in {"turn.completed", "turn.abandoned"}:
            outcome = (
                "abandoned" if kind == "turn.abandoned"
                else str(data.get("outcome") or "succeeded")
            )
            if outcome not in TURN_OUTCOMES:
                outcome = "failed"
            payload: dict[str, Any] = {"outcome": outcome}
            if outcome == "failed":
                payload["error"] = safe_error(
                    str(data.get("error_code") or "unknown"),
                    retryable=bool(data.get("retryable")),
                )
            return self._make(event, "turn.finished", payload)
        # Deliberate suppression: native frames, reasoning, context/token
        # diagnostics, tool payloads, credentials, and unrelated messages.
        return None

    def _tool(self, event: HarnessEvent, state: str) -> RuntimeEvent | None:
        data = event.data
        native_key = str(data.get("tool_call_ref") or "")
        key = (str(event.turn_ref), native_key)
        tool_ref = self._tool_refs.setdefault(
            key, f"tool_{uuid.uuid4().hex}"
        )
        return self._make(event, "tool.updated", {
            "tool_call_ref": tool_ref,
            "label": "Tool",
            "state": state,
        })

    def _block_ref(self, event: HarnessEvent) -> str:
        native_key = str(event.data.get("block_id") or "result")
        key = (str(event.turn_ref), native_key)
        return self._block_refs.setdefault(
            key, f"out_{uuid.uuid4().hex}"
        )

    def _prune_turn(self, turn_ref: str) -> None:
        """Drop the per-turn ref tables once the turn is terminal."""
        for refs in (self._block_refs, self._tool_refs):
            for key in [key for key in refs if key[0] == turn_ref]:
                del refs[key]
        self._started_blocks -= {
            key for key in self._started_blocks if key[0] == turn_ref
        }

    def project_all(self, event: HarnessEvent) -> tuple[RuntimeEvent, ...]:
        """Project an event while materializing output block boundaries.

        IDs are allocated only once each returned immutable event is final.
        Callers persisting a stream should use this method.
        """
        kind = (
            event.type.value
            if isinstance(event.type, HarnessEventType) else str(event.type)
        )
        projected = self.project(event)
        if projected is None:
            return ()
        if kind == "turn.started":
            # This is deliberately lifecycle-only.  The public activity must
            # not reflect normalized or native provider material.
            activity = RuntimeEvent(
                agent_id=projected.agent_id,
                session_ref=projected.session_ref,
                turn_ref=projected.turn_ref,
                type="activity.updated",
                payload={"text": "Working"},
                scope=projected.scope,
                # The companion lifecycle row must not consume a caller's
                # deterministic stream for subsequent normalized events.
                event_id=f"evt_{uuid.uuid4().hex}",
                occurred_at=projected.occurred_at,
            )
            return (projected, activity)
        if kind in {"turn.completed", "turn.abandoned"}:
            # Terminal boundary: no later event can reference this turn, so its
            # ref tables are dead weight for the rest of the daemon's life.
            self._prune_turn(projected.turn_ref)
            return (projected,)
        if kind == "turn.assistant_delta":
            key = (projected.turn_ref, str(projected.payload["block_id"]))
            started = self._started_blocks
            if key not in started:
                started.add(key)
                start = RuntimeEvent(
                    agent_id=projected.agent_id,
                    session_ref=projected.session_ref,
                    turn_ref=projected.turn_ref,
                    type="output.updated",
                    payload={
                        "block_id": projected.payload["block_id"],
                        "kind": projected.payload["kind"],
                        "phase": "start",
                    },
                    scope=projected.scope,
                    event_id=self.id_factory(),
                    occurred_at=projected.occurred_at,
                )
                return (start, projected)
        return (projected,)


class DeltaCoalescer:
    """Deterministically coalesce token deltas before IDs are allocated."""

    def __init__(self, max_bytes: int = 4096):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._buffer = ""

    def add(self, text: str) -> list[str]:
        self._buffer += text
        chunks: list[str] = []
        while len(self._buffer.encode("utf-8")) >= self.max_bytes:
            cut = self._byte_boundary(self._buffer, self.max_bytes)
            chunks.append(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
        return chunks

    def flush(self) -> list[str]:
        if not self._buffer:
            return []
        result, self._buffer = [self._buffer], ""
        return result

    @staticmethod
    def _byte_boundary(text: str, limit: int) -> int:
        used = 0
        for index, char in enumerate(text):
            size = len(char.encode("utf-8"))
            if used + size > limit:
                return max(1, index)
            used += size
        return len(text)
