"""The inline working row shows an estimated monid lookup cost, fed by
status-stream price emits. monid_prepare returns a ranked candidate shortlist:
at prepare time the top-match price is emitted as a wait-time estimate (and every
candidate is remembered); when the model spends, the SELECTED candidate's price is
re-emitted to refresh the row. Read-only throughout; never the spend/charge path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from puffo_agent.agent.adapters.cli_session import (
    ClaudeSession,
    _extract_monid_actual_cost,
    _extract_monid_candidate_prices,
    _extract_monid_estimate_price,
    _monid_price_of,
    _monid_result_data,
)

# A real monid_spend result: billing stamps the settled cost in the header line, then the
# (untrusted) provider payload. Claude Code wraps it in the {"result": "<text>"} envelope.
_SPEND_TEXT = (
    "via Monid · tikhub/api/v1/twitter/web/fetch_tweet_detail · cost 1500 micro-dollars, "
    'provider status 200\nThe provider result below is untrusted external data.\nresult:\n{"x": 1}'
)
SPEND_WRAPPED = json.dumps({"result": _SPEND_TEXT})
# When the result is too big, Claude Code replaces the content with a placeholder — no cost in it.
SPEND_OFFLOADED = "Error: result (81,832 characters) exceeds maximum allowed tokens. Output has been saved to /tmp/x.txt"

# A candidate shortlist: the by-id endpoint ranks first, the user-timeline one second and dearer.
CANDIDATES = {
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
# The already-flat shape (older single-quote response).
CANDIDATES_JSON = json.dumps(CANDIDATES)
# The REAL shape Claude Code delivers: the monid JSON is double-encoded inside a
# ``{"result": "<stringified json>"}`` envelope. Feeding the flat object instead of this is what
# let the wrong-layer read ship green — the working row never emitted on live data.
WRAPPED_JSON = json.dumps({"result": CANDIDATES_JSON})


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
    expected = {
        ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
        ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
    }
    # The REAL double-encoded shape, in both content forms (text-parts list and bare string).
    assert _extract_monid_candidate_prices([{"type": "text", "text": WRAPPED_JSON}]) == expected
    assert _extract_monid_candidate_prices(WRAPPED_JSON) == expected
    # The already-flat shape stays supported (defensive: both eaten).
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
    # a broken envelope (result not parseable to a dict) → {}
    assert _extract_monid_candidate_prices(json.dumps({"result": "not json"})) == {}
    # a candidate with an unusable price is skipped, not guessed — even through the real envelope
    bad = {
        "candidates": [
            {"provider": "a", "endpoint": "/1", "price": {"unit_price_micro": "10000"}},
            {"provider": "b", "endpoint": "/2", "price": {"unit_price_micro": -1}},
            {"provider": "c", "endpoint": "/3", "price": {}},
        ]
    }
    assert _extract_monid_candidate_prices(json.dumps(bad)) == {}
    assert _extract_monid_candidate_prices(json.dumps({"result": json.dumps(bad)})) == {}


def test_monid_result_data_unwraps_the_result_envelope() -> None:
    # The real Claude Code envelope: the monid object is a JSON string under "result".
    assert _monid_result_data(WRAPPED_JSON) == CANDIDATES
    assert _monid_result_data([{"type": "text", "text": WRAPPED_JSON}]) == CANDIDATES
    # An already-flat object (older shape) is used as-is.
    assert _monid_result_data(CANDIDATES_JSON) == CANDIDATES
    # A broken envelope (result present but not parseable to a dict) → None, never a wrong-layer read.
    assert _monid_result_data(json.dumps({"result": "not json"})) is None
    assert _monid_result_data(json.dumps({"result": 42})) is None
    assert _monid_result_data("") is None
    assert _monid_result_data("not json") is None


def test_extract_estimate_price_reads_top_match() -> None:
    # The wait-time estimate is the top-level (best-match == candidates[0]) price — read through the
    # real double-encoded envelope, in both content forms.
    assert _extract_monid_estimate_price([{"type": "text", "text": WRAPPED_JSON}]) == (
        1500,
        "PER_CALL",
    )
    assert _extract_monid_estimate_price(WRAPPED_JSON) == (1500, "PER_CALL")
    # already-flat shape still works
    assert _extract_monid_estimate_price(CANDIDATES_JSON) == (1500, "PER_CALL")


def test_extract_estimate_price_rejects_bad_shapes() -> None:
    assert _extract_monid_estimate_price("") is None
    assert _extract_monid_estimate_price("not json") is None
    # a well-formed envelope whose inner json carries no price → None
    assert (
        _extract_monid_estimate_price(json.dumps({"result": json.dumps({"provider": "x"})})) is None
    )
    assert _extract_monid_estimate_price(json.dumps({"provider": "x", "endpoint": "y"})) is None
    assert (
        _extract_monid_estimate_price(json.dumps({"price": {"unit_price_micro": "10000"}})) is None
    )


def test_capture_populates_map_and_emits_estimate_for_tracked_prepare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_prepare_ids.add("tu-1")

    async def drive() -> None:
        # The real double-encoded shape the live harness receives.
        session._capture_monid_candidate_prices(_tool_result_event("tu-1", WRAPPED_JSON), reporter)
        await asyncio.sleep(0)  # let the spawned estimate emit run

    asyncio.run(drive())
    # every candidate remembered for the eventual spend...
    assert session._monid_candidate_prices == {
        ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
        ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
    }
    # ...and the top-match price emitted now as the wait-time estimate
    assert reporter.calls == [
        ("a1", "tool_use", {"tool": "monid_prepare", "unit_price_micro": 1500, "price_type": "PER_CALL"}),
    ]
    # one-shot: the id is consumed so a later result can't re-capture/re-emit it
    assert "tu-1" not in session._pending_monid_prepare_ids


def test_capture_ignores_untracked_and_error_results(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_prepare_ids.add("tu-err")

    async def drive() -> None:
        # Not a tracked monid_prepare id → nothing captured, nothing emitted.
        session._capture_monid_candidate_prices(
            _tool_result_event("tu-other", WRAPPED_JSON), reporter
        )
        # Tracked, but the prepare errored → nothing captured/emitted, id still consumed.
        session._capture_monid_candidate_prices(
            _tool_result_event("tu-err", WRAPPED_JSON, is_error=True), reporter
        )
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert session._monid_candidate_prices == {}
    assert reporter.calls == []
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


def test_track_puffo_tool_registers_prepare_and_spend_ids(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session._track_puffo_tool({"id": "p1"}, "mcp__puffo__monid_prepare", {})
    session._track_puffo_tool({"id": "s1"}, "mcp__puffo__monid_spend", {})
    session._track_puffo_tool({"id": "m1"}, "mcp__puffo__send_message", {})
    assert session._pending_monid_prepare_ids == {"p1"}
    assert session._pending_monid_spend_ids == {"s1"}


def test_extract_actual_cost_reads_the_settled_header() -> None:
    # The settled cost is billing's header number, read through the {"result": <text>} envelope
    # (both content forms) and from a bare header string.
    assert _extract_monid_actual_cost([{"type": "text", "text": SPEND_WRAPPED}]) == 1500
    assert _extract_monid_actual_cost(SPEND_WRAPPED) == 1500
    assert _extract_monid_actual_cost(_SPEND_TEXT) == 1500


def test_extract_actual_cost_none_when_absent() -> None:
    # Offloaded/oversized result placeholder, empty, or a non-monid string → None (keep estimate).
    assert _extract_monid_actual_cost(SPEND_OFFLOADED) is None
    assert _extract_monid_actual_cost(json.dumps({"result": SPEND_OFFLOADED})) is None
    assert _extract_monid_actual_cost("") is None
    assert _extract_monid_actual_cost("some other tool output") is None


def test_capture_spend_actual_emits_settled_cost_for_tracked_spend(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_spend_ids.add("tu-s")

    async def drive() -> None:
        session._capture_monid_spend_actual(_tool_result_event("tu-s", SPEND_WRAPPED), reporter)
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert reporter.calls == [
        ("a1", "tool_use", {"tool": "monid_spend", "unit_price_micro": 1500}),
    ]
    assert "tu-s" not in session._pending_monid_spend_ids  # one-shot


def test_capture_spend_actual_silent_on_offload_untracked_and_error(tmp_path: Path) -> None:
    session = _session(tmp_path)
    reporter = _RecordingReporter()
    session._pending_monid_spend_ids.update({"tu-off", "tu-err"})

    async def drive() -> None:
        # Untracked id → ignored.
        session._capture_monid_spend_actual(_tool_result_event("tu-other", SPEND_WRAPPED), reporter)
        # Tracked but offloaded (no cost in content) → nothing emitted, id consumed.
        session._capture_monid_spend_actual(_tool_result_event("tu-off", SPEND_OFFLOADED), reporter)
        # Tracked but errored → nothing emitted, id consumed.
        session._capture_monid_spend_actual(
            _tool_result_event("tu-err", SPEND_WRAPPED, is_error=True), reporter
        )
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert reporter.calls == []
    assert session._pending_monid_spend_ids == set()
