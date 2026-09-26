"""The inline working row shows an estimated monid lookup cost, fed by a
status-stream emit of monid_prepare's quoted unit price. These cover the
result parsing and the projection that emits it (read-only; never the spend
path)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from puffo_agent.agent.adapters.cli_session import (
    ClaudeSession,
    _extract_monid_unit_price,
)

PRICE_JSON = json.dumps(
    {
        "provider": "x",
        "endpoint": "y",
        "price": {"price_type": "PER_CALL", "unit_price_micro": 10000},
    }
)


def _session(tmp_path: Path) -> ClaudeSession:
    return ClaudeSession(
        agent_id="a1",
        session_file=tmp_path / "session.json",
        build_command=lambda extra, env: [],
    )


class _RecordingReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def emit(self, agent_slug: str, event: str, payload: dict) -> None:
        self.calls.append((agent_slug, event, payload))


def _tool_result_event(tool_use_id: str, text: str, *, is_error: bool = False) -> dict:
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def test_extract_unit_price_reads_price_block() -> None:
    # Both content forms Claude Code emits: a list of text parts, or a string.
    assert _extract_monid_unit_price([{"type": "text", "text": PRICE_JSON}]) == (
        10000,
        "PER_CALL",
    )
    assert _extract_monid_unit_price(PRICE_JSON) == (10000, "PER_CALL")
    # price_type is optional.
    assert _extract_monid_unit_price(
        json.dumps({"price": {"unit_price_micro": 5}})
    ) == (5, None)


def test_extract_unit_price_rejects_bad_shapes() -> None:
    assert _extract_monid_unit_price("") is None
    assert _extract_monid_unit_price("not json") is None
    assert _extract_monid_unit_price(json.dumps({"price": {}})) is None
    # a string, not an int, must not be trusted as a price
    assert _extract_monid_unit_price(json.dumps({"price": {"unit_price_micro": "10000"}})) is None
    assert _extract_monid_unit_price(json.dumps({"price": {"unit_price_micro": -1}})) is None
    # bool is an int subclass — reject it explicitly
    assert _extract_monid_unit_price(json.dumps({"price": {"unit_price_micro": True}})) is None


def test_project_monid_price_emits_for_tracked_prepare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_prepare_ids.add("tu-1")

    async def drive() -> None:
        session._project_monid_price(_tool_result_event("tu-1", PRICE_JSON), reporter)
        await asyncio.sleep(0)  # let the spawned emit run

    asyncio.run(drive())
    assert reporter.calls == [
        (
            "a1",
            "tool_use",
            {"tool": "monid_prepare", "unit_price_micro": 10000, "price_type": "PER_CALL"},
        )
    ]
    # one-shot: the id is consumed so a later result can't re-emit it
    assert "tu-1" not in session._pending_monid_prepare_ids


def test_project_monid_price_ignores_untracked_and_error_results(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_prepare_ids.add("tu-err")

    async def drive() -> None:
        # Not a tracked monid_prepare id → nothing emitted.
        session._project_monid_price(_tool_result_event("tu-other", PRICE_JSON), reporter)
        # Tracked, but the prepare errored → nothing emitted, id still consumed.
        session._project_monid_price(
            _tool_result_event("tu-err", PRICE_JSON, is_error=True), reporter
        )
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert reporter.calls == []
    assert "tu-err" not in session._pending_monid_prepare_ids


def test_track_puffo_tool_registers_only_monid_prepare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session._track_puffo_tool({"id": "p1"}, "mcp__puffo__monid_prepare", {})
    session._track_puffo_tool({"id": "s1"}, "mcp__puffo__monid_spend", {})
    session._track_puffo_tool({"id": "m1"}, "mcp__puffo__send_message", {})
    assert session._pending_monid_prepare_ids == {"p1"}
