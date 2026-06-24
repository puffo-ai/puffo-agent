"""Per-agent audit-log capture for the codex adapter (PUF-324).

Operator-direct intake (Vase msg_9f82bfa2) — daemon audit-log was
missing the agent's intermediate "searching web", "updating code..."
narrative for codex-driven agents. The claude-code adapter already
captured these via ``ClaudeSession.audit`` (cli_session.py:746); the
codex path's ``_handle_item_event`` was un-instrumented, so the
streaming ``item/agentMessage/delta`` chunks flowed into the reply
buffer without ever hitting ``audit.write``.

These tests pin the cross-adapter contract: a CodexSession given an
``audit`` parameter emits the same NDJSON event shape
(``assistant.text`` for narrative, ``tool`` for tool invocations) so
operators can tail ``<workspace>/.puffo-agent/audit.log`` and see
agent activity regardless of which adapter the agent runs on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.codex_session import (
    CodexSession,
    _PendingTurn,
)


class _AuditSpy:
    """Stub matching ``AuditLog.write(event, **fields)``. Captures
    every call so tests can assert exact event names + payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def write(self, event: str, **fields: Any) -> None:
        self.calls.append((event, fields))


def _make_session(audit: Any = None) -> CodexSession:
    """Bare CodexSession with just enough state to drive
    ``_handle_item_event`` directly. Skips the on-disk
    session-id load + the asyncio subprocess wiring — they
    aren't on the per-event audit path under test."""
    session = CodexSession.__new__(CodexSession)
    session.agent_id = "agent-test"
    session.audit = audit
    return session


def _make_turn() -> _PendingTurn:
    """``_PendingTurn`` mirrors the in-flight ``sendUserTurn`` state
    (``reply_chunks`` / ``tool_calls`` / ``send_message_targets``).
    A bare instance is enough for the per-event handler."""
    return _PendingTurn(request_id=1, started_at=0.0)


# ─── streaming delta → assistant.text ────────────────────────────


@pytest.mark.asyncio
async def test_streaming_delta_writes_assistant_text_to_audit():
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    await session._handle_item_event(
        "item/agentMessage/delta",
        {"delta": "searching web for the answer"},
        turn,
    )

    assert audit.calls == [
        ("assistant.text", {"text": "searching web for the answer"}),
    ]
    # Reply buffer still gets the chunk so the final reply
    # composition keeps working — audit is a tee, not a replacement.
    assert turn.reply_chunks == ["searching web for the answer"]


@pytest.mark.asyncio
async def test_streaming_delta_with_empty_string_writes_nothing():
    """The reply-buffer guard at line 1094 already drops empty
    deltas; the audit tee respects the same guard so we don't get
    noise rows in audit.log."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    await session._handle_item_event(
        "item/agentMessage/delta", {"delta": ""}, turn,
    )

    assert audit.calls == []
    assert turn.reply_chunks == []


@pytest.mark.asyncio
async def test_streaming_delta_audit_is_optional():
    """``audit=None`` path (the default before PUF-324 wired it) must
    still work — no AttributeError, no behaviour change vs main."""
    session = _make_session(audit=None)
    turn = _make_turn()

    await session._handle_item_event(
        "item/agentMessage/delta", {"delta": "thinking..."}, turn,
    )

    assert turn.reply_chunks == ["thinking..."]


# ─── completed agent_message → missed-delta fallback ──────────────


@pytest.mark.asyncio
async def test_completed_agent_message_writes_when_delta_buffer_diverges():
    """codex_session.py:1109-1111 replaces ``turn.reply_chunks`` with
    the authoritative ``item.text`` when the concatenated deltas
    don't match it. In that case the streaming-delta audit rows are
    stale; emit a synthetic ``assistant.text`` so the audit log
    carries the operator-observable final narrative."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()
    turn.reply_chunks = ["partial"]  # a missed-delta scenario

    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "agentMessage", "text": "authoritative final"}},
        turn,
    )

    assert turn.reply_chunks == ["authoritative final"]
    assert audit.calls == [
        ("assistant.text", {"text": "authoritative final"}),
    ]


@pytest.mark.asyncio
async def test_completed_agent_message_no_audit_when_buffer_matches():
    """When the delta buffer DOES match the authoritative final text,
    the streaming-delta rows in audit.log are already correct — don't
    duplicate the row."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()
    turn.reply_chunks = ["authoritative final"]

    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "agentMessage", "text": "authoritative final"}},
        turn,
    )

    assert audit.calls == []


# ─── completed tool_use → tool ────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_tool_use_writes_tool_audit_row():
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    await session._handle_item_event(
        "item/completed",
        {
            "item": {
                "type": "toolUse",
                "name": "shell_exec",
                "input": {"cmd": "ls /tmp"},
                "id": "tu_001",
            },
        },
        turn,
    )

    assert audit.calls == [
        ("tool", {
            "name": "shell_exec",
            "input": {"cmd": "ls /tmp"},
            "id": "tu_001",
        }),
    ]
    assert turn.tool_calls == 1


# ─── completed mcpToolCall → tool with server__tool name ──────────


@pytest.mark.asyncio
async def test_completed_mcp_tool_call_writes_tool_audit_row_with_namespaced_name():
    """Real codex mcpToolCall events split tool name across two
    fields (server + tool). Audit row mirrors the claude-code shape
    by joining them with ``__`` — so a downstream grep of
    ``audit.log`` for ``puffo__send_message`` lights up on both
    adapters."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    await session._handle_item_event(
        "item/completed",
        {
            "item": {
                "type": "mcpToolCall",
                "server": "puffo",
                "tool": "send_message",
                "status": "completed",
                "arguments": {"channel": "ch_test", "text": "hi"},
                "id": "tu_002",
            },
        },
        turn,
    )

    assert audit.calls == [
        ("tool", {
            "name": "puffo__send_message",
            "input": {"channel": "ch_test", "text": "hi"},
            "id": "tu_002",
        }),
    ]
    assert turn.tool_calls == 1


# ─── audit=None is tolerated everywhere ───────────────────────────


@pytest.mark.asyncio
async def test_completed_paths_tolerate_audit_none():
    """Every audit.write site in the handler is guarded; ensure
    none accidentally regress to an unguarded call."""
    session = _make_session(audit=None)
    turn = _make_turn()

    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "agentMessage", "text": "x"}},
        turn,
    )
    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "toolUse", "name": "x", "input": {}}},
        turn,
    )
    await session._handle_item_event(
        "item/completed",
        {
            "item": {
                "type": "mcpToolCall",
                "server": "puffo",
                "tool": "send_message",
                "status": "completed",
                "arguments": {},
            },
        },
        turn,
    )
    # No assertion failure means none of the three branches raised
    # AttributeError on the missing audit.
