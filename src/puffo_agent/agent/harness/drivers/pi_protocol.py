"""Pinned Pi RPC command builders and exhaustive event normalization.

Protocol surface is pinned in ``tests/fixtures/pi_rpc_0_84_3.py`` and verified
against the published ``@earendil-works/pi-coding-agent@0.84.3`` tarball, not
the docs site.

Two Pi-specific facts drive the shape of this module:

* **``agent_settled`` is the only terminal.** ``docs/rpc.md`` states that
  ``agent_end`` completes "one low-level agent run" and "may still be followed
  by retry, compaction, or queued continuations", while ``agent_settled`` fires
  only once "no automatic retry, compaction retry, or queued continuation
  remains". Pi's own documented Python client breaks its loop on ``agent_end``;
  doing that here would finalize a Puffo turn while the model is still running,
  which is the unbound-turn failure we already fixed once.
* **Every documented event gets an explicit branch.** Returning ``()`` is a
  deliberate drop for a high-frequency frame that carries no Puffo signal;
  reaching the tail is a protocol change we have not read yet, and is reported
  as a warning rather than swallowed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...provider_failures import classify_provider_failure
from ..driver import (
    HarnessEvent,
    HarnessEventType,
    RuntimeSpec,
    SessionRef,
    TurnRef,
)

# Agent -> client frames that are not in the documented event table: a command
# response, and the extension UI sub-protocol.
RESPONSE_FRAME = "response"
EXTENSION_UI_REQUEST = "extension_ui_request"

# Dialog methods block the agent until the client answers on stdin. Puffo has
# no interactive operator at this layer, so an unanswered dialog is a stall,
# not a no-op.
EXTENSION_UI_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

# Fire-and-forget methods must NOT be answered; a response to one would be an
# unmatched id on the agent side.
EXTENSION_UI_FIRE_AND_FORGET_METHODS = frozenset({
    "notify", "setStatus", "setWidget", "setTitle", "set_editor_text",
})

# Terminal event for a Puffo turn. Exactly one, deliberately.
TERMINAL_EVENT = "agent_settled"

# Enumerable production dispatch surface. The normalizer admits only these
# event names, and every admitted name must reach an explicit branch below.
# Tests compare these keys with the independently pinned upstream fixture.
PI_EVENT_DISPATCH_KEYS = frozenset({
    "agent_start", "agent_end", "agent_settled",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
    "bash_execution_update",
    "queue_update",
    "compaction_start", "compaction_end",
    "auto_retry_start", "auto_retry_end",
    "summarization_retry_scheduled",
    "summarization_retry_attempt_start",
    "summarization_retry_finished",
    "extension_error",
})


def build_pi_launch_command(spec: RuntimeSpec) -> tuple[str, ...]:
    """Build the persistent ``pi --mode rpc`` child command.

    Session persistence stays enabled: ``--no-session`` would make
    ``switch_session`` resume impossible, so a driver declaring
    ``session_resume=True`` must never pass it.
    """
    command = [spec.executable or "pi", "--mode", "rpc"]
    if spec.model:
        command.extend(("--model", spec.model))
    command.extend(spec.launch_args)
    return tuple(command)


def normalize_pi_event(
    frame: dict[str, Any],
    *,
    session_ref: SessionRef,
    turn_ref: TurnRef | None,
    native_session_id: str = "",
    native_turn_id: str = "",
) -> tuple[HarnessEvent, ...]:
    """Normalize one documented Pi RPC event line.

    Never terminates a turn: ``agent_settled`` is mapped by the driver, which
    owns turn state. This function only describes what a frame means.
    """
    type_ = str(frame.get("type") or "")

    def event(
        type_: HarnessEventType, data: dict[str, Any]
    ) -> HarnessEvent:
        return HarnessEvent.normalized(
            type=type_,
            driver="pi",
            session_ref=session_ref,
            turn_ref=turn_ref,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            data=data,
            native_payload=frame,
        )

    def record(record_type: str, **data: Any) -> tuple[HarnessEvent, ...]:
        return (
            event(
                HarnessEventType.SESSION_UPDATED,
                {"record_type": record_type, **data},
            ),
        )

    def unknown_event() -> tuple[HarnessEvent, ...]:
        return (
            event(
                HarnessEventType.RUNTIME_WARNING,
                {
                    "code": "unknown_pi_event",
                    "record_type": type_ or "unknown",
                },
            ),
        )

    if type_ not in PI_EVENT_DISPATCH_KEYS:
        return unknown_event()

    normalized = _normalize_pi_lifecycle(type_, frame, event, record)
    if normalized is not None:
        return normalized
    normalized = _normalize_pi_tool_event(type_, frame, event, record)
    if normalized is not None:
        return normalized
    normalized = _normalize_pi_runtime_event(type_, frame, event, record)
    if normalized is not None:
        return normalized

    # A dispatch key without a branch is a production programming error. It is
    # deliberately distinct from an upstream protocol event we do not know.
    raise AssertionError(
        f"Pi event {type_!r} is admitted but has no normalization branch"
    )


EventBuilder = Callable[[HarnessEventType, dict[str, Any]], HarnessEvent]
RecordBuilder = Callable[..., tuple[HarnessEvent, ...]]


def _normalize_pi_lifecycle(
    type_: str,
    frame: dict[str, Any],
    event: EventBuilder,
    record: RecordBuilder,
) -> tuple[HarnessEvent, ...] | None:
    if type_ == "agent_start":
        return (event(HarnessEventType.TURN_STARTED, {}),)
    if type_ == "agent_end":
        # Explicitly NOT terminal. See module docstring.
        return record("agent_end", will_retry=bool(frame.get("willRetry")))
    if type_ == "agent_settled":
        # The driver converts this into TURN_COMPLETED; it owns the turn state
        # and the accumulated outcome, which a stateless normalizer cannot see.
        return ()

    # A Pi "turn" is one assistant response plus its tool calls, so several
    # occur inside one Puffo turn. Mapping these to TURN_STARTED/TURN_COMPLETED
    # would emit multiple boundaries per Puffo turn.
    if type_ == "turn_start":
        return record("turn_start")
    if type_ == "turn_end":
        return record("turn_end")

    # --- messages ------------------------------------------------------------
    if type_ == "message_start":
        return record("message_start", role=_message_role(frame))
    if type_ == "message_update":
        return _normalize_message_update(frame, event)
    if type_ == "message_end":
        events: list[HarnessEvent] = [
            event(HarnessEventType.ASSISTANT_COMPLETED, {"block_id": ""})
        ]
        usage = _usage_data(frame.get("usage"))
        if usage:
            events.append(event(HarnessEventType.CONTEXT_UPDATED, usage))
        return tuple(events)
    return None


def _normalize_pi_tool_event(
    type_: str,
    frame: dict[str, Any],
    event: EventBuilder,
    record: RecordBuilder,
) -> tuple[HarnessEvent, ...] | None:
    if type_ == "tool_execution_start":
        return (
            event(
                HarnessEventType.TOOL_STARTED,
                {
                    "tool_call_ref": str(frame.get("toolCallId") or ""),
                    "label": _tool_label(frame),
                },
            ),
        )
    if type_ == "tool_execution_update":
        return (
            event(
                HarnessEventType.TOOL_UPDATED,
                {
                    "tool_call_ref": str(frame.get("toolCallId") or ""),
                    "label": _tool_label(frame),
                },
            ),
        )
    if type_ == "tool_execution_end":
        return (
            event(
                HarnessEventType.TOOL_COMPLETED,
                {
                    "tool_call_ref": str(frame.get("toolCallId") or ""),
                    "label": _tool_label(frame),
                    "outcome": (
                        "failed" if frame.get("isError") else "succeeded"
                    ),
                },
            ),
        )

    # Output of the direct ``bash`` RPC command, which this driver never sends.
    # Recorded rather than dropped so an unexpected one stays visible.
    if type_ == "bash_execution_update":
        return record("bash_execution_update")

    # --- queue ---------------------------------------------------------------
    if type_ == "queue_update":
        # Counts only: the queued entries are user message text.
        return record(
            "queue_update",
            steering=_length(frame.get("steering")),
            follow_up=_length(frame.get("followUp")),
        )
    return None


def _normalize_pi_runtime_event(
    type_: str,
    frame: dict[str, Any],
    event: EventBuilder,
    record: RecordBuilder,
) -> tuple[HarnessEvent, ...] | None:
    if type_ == "compaction_start":
        return (
            event(
                HarnessEventType.COMPACTION_STARTED,
                {"reason": str(frame.get("reason") or "")},
            ),
        )
    if type_ == "compaction_end":
        return (
            event(
                HarnessEventType.COMPACTION_COMPLETED,
                _compaction_end_data(frame),
            ),
        )

    # --- retries -------------------------------------------------------------
    if type_ == "auto_retry_start":
        return (
            event(
                HarnessEventType.RUNTIME_WARNING,
                {
                    "code": "auto_retry",
                    "attempt": _nonnegative_int(frame.get("attempt")),
                    "max_attempts": _nonnegative_int(frame.get("maxAttempts")),
                    "delay_ms": _nonnegative_int(frame.get("delayMs")),
                    "failure_code": _failure_code(frame.get("errorMessage")),
                },
            ),
        )
    if type_ == "auto_retry_end":
        succeeded = bool(frame.get("success"))
        data = {
            "code": "auto_retry_end",
            "attempt": _nonnegative_int(frame.get("attempt")),
            "succeeded": succeeded,
        }
        if not succeeded:
            data["failure_code"] = _failure_code(frame.get("finalError"))
        return (event(HarnessEventType.RUNTIME_WARNING, data),)
    if type_ == "summarization_retry_scheduled":
        return (
            event(
                HarnessEventType.RUNTIME_WARNING,
                {
                    "code": "summarization_retry",
                    "attempt": _nonnegative_int(frame.get("attempt")),
                    "max_attempts": _nonnegative_int(frame.get("maxAttempts")),
                    "delay_ms": _nonnegative_int(frame.get("delayMs")),
                    "failure_code": _failure_code(frame.get("errorMessage")),
                },
            ),
        )
    if type_ == "summarization_retry_attempt_start":
        return record(
            "summarization_retry_attempt_start",
            source=str(frame.get("source") or ""),
            reason=str(frame.get("reason") or ""),
        )
    if type_ == "summarization_retry_finished":
        return record("summarization_retry_finished")

    # --- extensions ----------------------------------------------------------
    if type_ == "extension_error":
        # ``extensionPath`` is a local filesystem path and ``error`` is
        # arbitrary text; neither is copied into the public event.
        return (
            event(
                HarnessEventType.RUNTIME_WARNING,
                {
                    "code": "extension_error",
                    "extension_event": str(frame.get("event") or ""),
                },
            ),
        )
    return None


def _normalize_message_update(
    frame: dict[str, Any], event: Any
) -> tuple[HarnessEvent, ...]:
    """Branch on ``assistantMessageEvent``; usage here is not context status.

    The top-level ``usage`` is cumulative provider usage, a different quantity
    from ``contextUsage`` in ``get_session_stats``. Pushing it as context status
    would report the wrong number, which is why this driver declares
    ``context_status=PULL``.
    """
    delta = frame.get("assistantMessageEvent")
    delta = delta if isinstance(delta, dict) else {}
    delta_type = str(delta.get("type") or "")
    block_id = _block_id(delta)

    if delta_type == "text_start":
        return ()
    if delta_type == "text_delta":
        return (
            event(
                HarnessEventType.ASSISTANT_DELTA,
                {"block_id": block_id, "delta": str(delta.get("delta") or "")},
            ),
        )
    if delta_type == "text_end":
        return (
            event(HarnessEventType.ASSISTANT_COMPLETED, {"block_id": block_id}),
        )
    # Thinking content is deliberately not surfaced as assistant output.
    if delta_type in ("thinking_start", "thinking_delta", "thinking_end"):
        return ()
    if delta_type == "toolcall_start":
        return (
            event(
                HarnessEventType.TOOL_STARTED,
                {
                    "tool_call_ref": str(delta.get("id") or ""),
                    "label": _label(str(delta.get("toolName") or "")),
                },
            ),
        )
    # Argument chunks carry no Puffo signal and arrive at token frequency;
    # ``tool_execution_*`` is the authoritative tool lifecycle.
    if delta_type in ("toolcall_delta", "toolcall_end"):
        return ()
    return (
        event(
            HarnessEventType.RUNTIME_WARNING,
            {
                "code": "unknown_pi_message_delta",
                "record_type": delta_type or "unknown",
            },
        ),
    )


def _compaction_end_data(frame: dict[str, Any]) -> dict[str, Any]:
    result = frame.get("result")
    aborted = bool(frame.get("aborted"))
    data: dict[str, Any] = {
        "reason": str(frame.get("reason") or ""),
        "will_retry": bool(frame.get("willRetry")),
        "aborted": aborted,
    }
    if isinstance(result, dict):
        data["outcome"] = "succeeded"
        data["tokens_before"] = _nonnegative_int(result.get("tokensBefore"))
        data["estimated_tokens_after"] = _nonnegative_int(
            result.get("estimatedTokensAfter")
        )
        return data
    # result is null: either aborted, or a real failure carrying errorMessage.
    data["outcome"] = "cancelled" if aborted else "failed"
    if not aborted:
        data["failure_code"] = _failure_code(frame.get("errorMessage"))
    return data


def _failure_code(message: Any) -> str:
    """Classify provider text into the shared vocabulary; never copy it."""
    if not isinstance(message, str) or not message.strip():
        return "provider_error"
    return classify_provider_failure(status=None, diagnostic=message)


def _message_role(frame: dict[str, Any]) -> str:
    message = frame.get("message")
    message = message if isinstance(message, dict) else {}
    return str(message.get("role") or "")


def _block_id(delta: dict[str, Any]) -> str:
    index = delta.get("contentIndex")
    if isinstance(index, bool) or not isinstance(index, int):
        return ""
    return str(index)


def _tool_label(frame: dict[str, Any]) -> str:
    return _label(str(frame.get("toolName") or ""))


def _label(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def _length(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def _usage_data(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": _nonnegative_int(usage.get("input")),
        "output_tokens": _nonnegative_int(usage.get("output")),
        "cache_read_tokens": _nonnegative_int(usage.get("cacheRead")),
        "cache_write_tokens": _nonnegative_int(usage.get("cacheWrite")),
        "context_tokens": _nonnegative_int(usage.get("totalTokens")),
    }


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
