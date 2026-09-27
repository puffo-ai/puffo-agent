"""The codex path of the estimated-monid-cost surface: off a TOOL_COMPLETED
event the legacy status projector emits the top-match price at monid_prepare as
a wait-time estimate (and remembers every candidate), then re-emits the SELECTED
capability's price when the model spends — both as ``tool_use`` events on the
status stream the working row reads. Parallel to the Claude side (cli_session);
read-only, never the spend path."""

from __future__ import annotations

import json

from puffo_agent.agent.harness.driver import (
    HarnessEvent,
    HarnessEventType,
    SessionRef,
    TurnRef,
)
from puffo_agent.agent.harness.runtime import local_runtime
from puffo_agent.agent.harness.runtime.local_runtime import (
    _LegacyStatusProjector,
    _codex_result_text,
    _monid_candidate_prices_from_native,
    _monid_estimate_price_from_native,
    _monid_spend_target,
)

# A candidate shortlist: the by-id endpoint ranks first, the user-timeline one second and dearer.
CANDIDATES = {
    "provider": "tikhub",
    "endpoint": "/fetch_tweet_detail",
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
CANDIDATES_JSON = json.dumps(CANDIDATES)
# The REAL shape a codex mcpToolCall completed result carries: the monid JSON is text inside an MCP
# result envelope (content parts), not a bare object. Feeding a bare object hid the wrong-layer read.
MCP_ENVELOPE = {"content": [{"type": "text", "text": CANDIDATES_JSON}], "isError": False}
_EXPECTED = {
    ("tikhub", "/fetch_tweet_detail"): (1500, "PER_CALL"),
    ("tikhub", "/fetch_user_tweet_replies"): (3000, "PER_RESULT"),
}


def test_codex_result_text_flattens_every_shape() -> None:
    # A plain string result, an MCP content list, and the dict wrappers codex
    # may hand back all flatten to the same JSON text.
    assert _codex_result_text(CANDIDATES_JSON) == CANDIDATES_JSON
    assert _codex_result_text([{"type": "text", "text": CANDIDATES_JSON}]) == CANDIDATES_JSON
    assert (
        _codex_result_text({"content": [{"type": "text", "text": CANDIDATES_JSON}]})
        == CANDIDATES_JSON
    )
    assert _codex_result_text({"contentItems": [{"text": CANDIDATES_JSON}]}) == CANDIDATES_JSON
    # Nothing usable → empty (never raises).
    assert _codex_result_text(None) == ""
    assert _codex_result_text(12345) == ""
    assert _codex_result_text({"other": 1}) == ""


def test_candidate_prices_from_native_reads_every_normal_shape() -> None:
    # The result may arrive as an MCP result envelope (the REAL mcpToolCall shape), a JSON string,
    # bare MCP content parts, or an already-parsed dict — each must yield the per-candidate map.
    assert _monid_candidate_prices_from_native({"result": MCP_ENVELOPE}) == _EXPECTED
    assert _monid_candidate_prices_from_native({"result": CANDIDATES_JSON}) == _EXPECTED
    assert (
        _monid_candidate_prices_from_native({"result": [{"type": "text", "text": CANDIDATES_JSON}]})
        == _EXPECTED
    )
    assert _monid_candidate_prices_from_native({"result": CANDIDATES}) == _EXPECTED
    # An older single-quote response (no candidates array) still yields its one entry.
    single = {"provider": "x", "endpoint": "y", "price": {"unit_price_micro": 5}}
    assert _monid_candidate_prices_from_native({"result": single}) == {("x", "y"): (5, None)}


def test_candidate_prices_from_native_is_fail_safe_across_shapes() -> None:
    # The codex result shape is not empirically pinned, so an entry that can't yield a clean price
    # is skipped (→ no emit → blank row, never a guessed/0 price).
    assert _monid_candidate_prices_from_native({"result": "not json"}) == {}
    assert _monid_candidate_prices_from_native({"result": [{"type": "text", "text": "hi"}]}) == {}
    assert _monid_candidate_prices_from_native({"result": {"other": 1}}) == {}
    # an errored prepare — even with a well-formed body — yields nothing
    assert _monid_candidate_prices_from_native({"result": CANDIDATES_JSON, "is_error": True}) == {}
    # an omitted result (dynamicToolCall success with result None)
    assert _monid_candidate_prices_from_native({"result": None, "result_omitted": True}) == {}
    assert _monid_candidate_prices_from_native(None) == {}
    assert _monid_candidate_prices_from_native({"result": ""}) == {}
    # string / bool / negative micros are not trustworthy → those candidates dropped
    bad = json.dumps(
        {
            "candidates": [
                {"provider": "a", "endpoint": "/1", "price": {"unit_price_micro": "10000"}},
                {"provider": "b", "endpoint": "/2", "price": {"unit_price_micro": True}},
                {"provider": "c", "endpoint": "/3", "price": {"unit_price_micro": -1}},
            ]
        }
    )
    assert _monid_candidate_prices_from_native({"result": bad}) == {}


def test_estimate_price_from_native_reads_top_match() -> None:
    # The wait-time estimate is the top-level (best-match == candidates[0]) price — read through the
    # REAL MCP result envelope, and through the older bare shapes too.
    assert _monid_estimate_price_from_native({"result": MCP_ENVELOPE}) == (1500, "PER_CALL")
    assert _monid_estimate_price_from_native({"result": CANDIDATES_JSON}) == (1500, "PER_CALL")
    assert _monid_estimate_price_from_native({"result": CANDIDATES}) == (1500, "PER_CALL")
    # fail-safe: errored / bad shape / no clean top-level price → None
    assert _monid_estimate_price_from_native({"result": CANDIDATES_JSON, "is_error": True}) is None
    # an MCP envelope whose inner text is not parseable → None (not a wrong-layer read of the shell)
    assert (
        _monid_estimate_price_from_native(
            {"result": {"content": [{"type": "text", "text": "not json"}]}}
        )
        is None
    )
    assert _monid_estimate_price_from_native({"result": "not json"}) is None
    assert _monid_estimate_price_from_native({"result": {"provider": "x", "endpoint": "y"}}) is None
    assert _monid_estimate_price_from_native(None) is None


def test_spend_target_reads_arguments() -> None:
    assert _monid_spend_target({"arguments": {"provider": "p", "endpoint": "/e"}}) == ("p", "/e")
    assert _monid_spend_target({"arguments": {"provider": "p"}}) is None  # endpoint missing
    assert _monid_spend_target({"arguments": {}}) is None
    assert _monid_spend_target({}) is None
    assert _monid_spend_target(None) is None


def _completed(label: str, native: dict, *, ref: str = "tool-1") -> HarnessEvent:
    return HarnessEvent.normalized(
        type=HarnessEventType.TOOL_COMPLETED,
        driver="codex",
        session_ref=SessionRef("s1"),
        turn_ref=TurnRef("t1"),
        data={"tool_call_ref": ref, "label": label, "outcome": "succeeded"},
        native_payload=native,
    )


def _drive(projector: _LegacyStatusProjector, *events: HarnessEvent) -> None:
    projector.project(
        HarnessEvent(
            type=HarnessEventType.TURN_STARTED,
            driver="codex",
            session_ref=SessionRef("s1"),
            turn_ref=TurnRef("t1"),
        )
    )
    for event in events:
        projector.project(event)


def test_projector_emits_selected_candidate_price_on_spend(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime,
        "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    _drive(
        projector,
        _completed("monid_prepare", {"result": MCP_ENVELOPE}, ref="p1"),
        # The model spends on the SECOND (lower-ranked) candidate.
        _completed(
            "monid_spend",
            {"arguments": {"provider": "tikhub", "endpoint": "/fetch_user_tweet_replies"}},
            ref="s1",
        ),
    )
    # prepare emits the top-match wait-time estimate; the spend then refreshes to the selected one.
    assert calls == [
        ("a1", "tool_use", {"tool": "monid_prepare", "unit_price_micro": 1500, "price_type": "PER_CALL"}),
        ("a1", "tool_use", {"tool": "monid_spend", "unit_price_micro": 3000, "price_type": "PER_RESULT"}),
    ]


def test_projector_prepare_emits_top_match_estimate(monkeypatch) -> None:
    # prepare alone emits the top-match price as the wait-time estimate (shown even if no spend).
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime,
        "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    _drive(projector, _completed("monid_prepare", {"result": MCP_ENVELOPE}, ref="p1"))
    assert calls == [
        ("a1", "tool_use", {"tool": "monid_prepare", "unit_price_micro": 1500, "price_type": "PER_CALL"}),
    ]


def test_projector_price_emits_are_idempotent_per_tool_ref(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime,
        "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    prepare = _completed("monid_prepare", {"result": MCP_ENVELOPE}, ref="p1")
    spend = _completed(
        "monid_spend",
        {"arguments": {"provider": "tikhub", "endpoint": "/fetch_tweet_detail"}},
        ref="s1",
    )
    # Re-delivered prepare/spend frames for the same tool ref must not re-emit.
    _drive(projector, prepare, prepare, spend, spend)
    assert calls == [
        ("a1", "tool_use", {"tool": "monid_prepare", "unit_price_micro": 1500, "price_type": "PER_CALL"}),
        ("a1", "tool_use", {"tool": "monid_spend", "unit_price_micro": 1500, "price_type": "PER_CALL"}),
    ]


def test_projector_ignores_errored_prepare_and_unknown_spend_target(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime,
        "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    _drive(
        projector,
        # errored prepare → nothing remembered
        _completed("monid_prepare", {"result": CANDIDATES_JSON, "is_error": True}, ref="p1"),
        # spend on a target not in the (empty) remembered map → nothing emitted
        _completed(
            "monid_spend",
            {"arguments": {"provider": "tikhub", "endpoint": "/fetch_tweet_detail"}},
            ref="s1",
        ),
    )
    assert calls == []
