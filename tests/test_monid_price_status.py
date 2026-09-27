"""The inline working row shows an estimated monid lookup cost, fed by a
status-stream emit of the SELECTED capability's quoted unit price. monid_prepare
now returns a ranked candidate shortlist, so the price is remembered per
candidate at prepare time and emitted when the model spends on the one it chose
(read-only; never the spend/charge path)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from puffo_agent.agent.adapters.cli_session import (
    ClaudeSession,
    _extract_monid_candidate_prices,
    _monid_price_of,
)

# A candidate shortlist: the by-id endpoint ranks first, the user-timeline one second and dearer.
CANDIDATES_JSON = json.dumps(
    {
        "provider": "tikhub",
        "endpoint": "/fetch_tweet_detail",
        "category": "general",
        "price": {"price_type": "PER_CALL", "unit_price_micro": 1500},
        "candidates": [
            {
                "provider": "tikhub",
                "endpoint": "/fetch_tweet_detail",
                "price": {"price_type": "PER_CALL", "unit_price_micro": 1500},
            },
            {
                "provider": "tikhub",
                "endpoint": "/fetch_user_tweet_replies",
                "price": {"price_type": "PER_RESULT", "unit_price_micro": 3000},
            },
        ],
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


def test_price_of_reads_and_rejects_bad_shapes() -> None:
    assert _monid_price_of(
        {"price": {"price_type": "PER_CALL", "unit_price_micro": 10000}}
    ) == (10000, "PER_CALL")
    assert _monid_price_of({"price": {"unit_price_micro": 5}}) == (5, None)  # price_type optional
    assert _monid_price_of({}) is None
    assert _monid_price_of({"price": {}}) is None
    # a string, not an int, must not be trusted as a price
    assert _monid_price_of({"price": {"unit_price_micro": "10000"}}) is None
    assert _monid_price_of({"price": {"unit_price_micro": -1}}) is None
    # bool is an int subclass — reject it explicitly
    assert _monid_price_of({"price": {"unit_price_micro": True}}) is None


def test_extract_candidate_prices_maps_every_candidate() -> None:
    # Both content forms Claude Code emits: a list of text parts, or a bare string.
    expected = {
        ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
        ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
    }
    assert _extract_monid_candidate_prices([{"type": "text", "text": CANDIDATES_JSON}]) == expected
    assert _extract_monid_candidate_prices(CANDIDATES_JSON) == expected


def test_extract_candidate_prices_falls_back_to_top_level_only() -> None:
    # An older single-quote response (no candidates array) still yields its one entry.
    single = json.dumps(
        {"provider": "x", "endpoint": "y", "price": {"price_type": "PER_CALL", "unit_price_micro": 7}}
    )
    assert _extract_monid_candidate_prices(single) == {("x", "y"): (7, "PER_CALL")}


def test_extract_candidate_prices_rejects_bad_shapes() -> None:
    assert _extract_monid_candidate_prices("") == {}
    assert _extract_monid_candidate_prices("not json") == {}
    # a candidate with an unusable price is skipped, not guessed
    bad = json.dumps(
        {
            "candidates": [
                {"provider": "a", "endpoint": "/1", "price": {"unit_price_micro": "10000"}},
                {"provider": "b", "endpoint": "/2", "price": {"unit_price_micro": -1}},
                {"provider": "c", "endpoint": "/3", "price": {}},
            ]
        }
    )
    assert _extract_monid_candidate_prices(bad) == {}


def test_capture_populates_map_for_tracked_prepare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session._pending_monid_prepare_ids.add("tu-1")
    session._capture_monid_candidate_prices(_tool_result_event("tu-1", CANDIDATES_JSON))
    assert session._monid_candidate_prices == {
        ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
        ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
    }
    # one-shot: the id is consumed so a later result can't re-capture it
    assert "tu-1" not in session._pending_monid_prepare_ids


def test_capture_ignores_untracked_and_error_results(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session._pending_monid_prepare_ids.add("tu-err")
    # Not a tracked monid_prepare id → nothing captured.
    session._capture_monid_candidate_prices(_tool_result_event("tu-other", CANDIDATES_JSON))
    # Tracked, but the prepare errored → nothing captured, id still consumed.
    session._capture_monid_candidate_prices(
        _tool_result_event("tu-err", CANDIDATES_JSON, is_error=True)
    )
    assert session._monid_candidate_prices == {}
    assert "tu-err" not in session._pending_monid_prepare_ids


def test_emit_spend_price_emits_the_selected_candidate(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._monid_candidate_prices = {
        ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
        ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
    }

    async def drive() -> None:
        # The model spends on the SECOND (lower-ranked) candidate.
        session._emit_monid_spend_price(
            {"provider": "tikhub", "endpoint": "/fetch_user_tweet_replies"}, reporter
        )
        await asyncio.sleep(0)  # let the spawned emit run

    asyncio.run(drive())
    assert reporter.calls == [
        (
            "a1",
            "tool_use",
            {"tool": "monid_spend", "unit_price_micro": 3000, "price_type": "PER_RESULT"},
        )
    ]


def test_emit_spend_price_blank_when_target_not_in_candidates(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._monid_candidate_prices = {("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL")}

    async def drive() -> None:
        # Spending on something not in the remembered candidates → no price emitted (blank row).
        session._emit_monid_spend_price({"provider": "who", "endpoint": "/dis"}, reporter)
        # Missing/non-string args are also ignored.
        session._emit_monid_spend_price({"provider": "tikhub"}, reporter)
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert reporter.calls == []


def test_track_puffo_tool_registers_only_monid_prepare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session._track_puffo_tool({"id": "p1"}, "mcp__puffo__monid_prepare", {})
    session._track_puffo_tool({"id": "s1"}, "mcp__puffo__monid_spend", {})
    session._track_puffo_tool({"id": "m1"}, "mcp__puffo__send_message", {})
    assert session._pending_monid_prepare_ids == {"p1"}
