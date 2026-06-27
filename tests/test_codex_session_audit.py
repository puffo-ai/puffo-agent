"""Audit-log capture in the codex adapter (cross-adapter contract with ClaudeSession)."""

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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def write(self, event: str, **fields: Any) -> None:
        self.calls.append((event, fields))


def _make_session(audit: Any = None) -> CodexSession:
    session = CodexSession.__new__(CodexSession)
    session.agent_id = "agent-test"
    session.audit = audit
    return session


def _make_turn() -> _PendingTurn:
    return _PendingTurn(request_id=1, started_at=0.0)


# ─── streaming delta buffers, completion emits one assistant.text ─


@pytest.mark.asyncio
async def test_streaming_delta_buffers_but_does_not_write_audit():
    """Deltas accumulate in reply_chunks; audit row waits for completion."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    for chunk in ("[", "S", "IL", "ENT", "]"):
        await session._handle_item_event(
            "item/agentMessage/delta", {"delta": chunk}, turn,
        )

    assert audit.calls == []
    assert turn.reply_chunks == ["[", "S", "IL", "ENT", "]"]


@pytest.mark.asyncio
async def test_streaming_delta_with_empty_string_writes_nothing():
    """Empty delta → no buffer append."""
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
    """``audit=None`` must not crash."""
    session = _make_session(audit=None)
    turn = _make_turn()

    await session._handle_item_event(
        "item/agentMessage/delta", {"delta": "thinking..."}, turn,
    )

    assert turn.reply_chunks == ["thinking..."]


# ─── completed agent_message → single assistant.text row ──────────


@pytest.mark.asyncio
async def test_completed_agent_message_emits_one_audit_row_from_buffer():
    """Completion fires one ``assistant.text`` row with the assembled text."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()
    turn.reply_chunks = ["[", "S", "IL", "ENT", "]"]

    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "agentMessage", "text": "[SILENT]"}},
        turn,
    )

    assert audit.calls == [("assistant.text", {"text": "[SILENT]"})]


@pytest.mark.asyncio
async def test_completed_agent_message_writes_when_delta_buffer_diverges():
    """Missed-delta → buffer replaced + row uses authoritative text."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()
    turn.reply_chunks = ["partial"]

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
async def test_completed_agent_message_with_empty_text_writes_nothing():
    """Empty completion + empty buffer → no audit row."""
    audit = _AuditSpy()
    session = _make_session(audit)
    turn = _make_turn()

    await session._handle_item_event(
        "item/completed",
        {"item": {"type": "agentMessage", "text": ""}},
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
    """mcpToolCall → mcp__server__tool, matching the claude-code adapter."""
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
            "name": "mcp__puffo__send_message",
            "input": {"channel": "ch_test", "text": "hi"},
            "id": "tu_002",
        }),
    ]
    assert turn.tool_calls == 1


# ─── audit=None is tolerated everywhere ───────────────────────────


@pytest.mark.asyncio
async def test_completed_paths_tolerate_audit_none():
    """No audit.write site can unguard-fail when audit=None."""
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
