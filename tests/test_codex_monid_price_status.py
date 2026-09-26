"""The codex path of the estimated-monid-cost surface: the legacy status
projector reads monid_prepare's quoted unit price off a TOOL_COMPLETED event
and emits it as a second ``tool_use`` on the same status stream the working row
reads. Parallel to the Claude side (cli_session); read-only, never the spend
path."""

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
    _monid_price_from_native,
)

PRICE = {"price": {"price_type": "PER_CALL", "unit_price_micro": 10000}}
PRICE_JSON = json.dumps(PRICE)


def test_codex_result_text_flattens_every_shape() -> None:
    # A plain string result, an MCP content list, and the dict wrappers codex
    # may hand back all flatten to the same JSON text.
    assert _codex_result_text(PRICE_JSON) == PRICE_JSON
    assert _codex_result_text([{"type": "text", "text": PRICE_JSON}]) == PRICE_JSON
    assert _codex_result_text({"content": [{"type": "text", "text": PRICE_JSON}]}) == PRICE_JSON
    assert _codex_result_text({"contentItems": [{"text": PRICE_JSON}]}) == PRICE_JSON
    # Nothing usable → empty (never raises).
    assert _codex_result_text(None) == ""
    assert _codex_result_text(12345) == ""
    assert _codex_result_text({"other": 1}) == ""


def test_monid_price_from_native_reads_price_from_every_normal_shape() -> None:
    # The result may arrive as a JSON string, MCP content parts, or an
    # already-parsed dict — each normal form must yield the price.
    assert _monid_price_from_native({"result": PRICE_JSON}) == (10000, "PER_CALL")
    assert _monid_price_from_native(
        {"result": [{"type": "text", "text": PRICE_JSON}]}
    ) == (10000, "PER_CALL")
    assert _monid_price_from_native({"result": PRICE}) == (10000, "PER_CALL")
    # price_type is optional.
    assert _monid_price_from_native(
        {"result": json.dumps({"price": {"unit_price_micro": 5}})}
    ) == (5, None)


def test_monid_price_from_native_is_fail_safe_across_codex_result_shapes() -> None:
    # The codex result shape is not empirically pinned, so the extractor must
    # return None (→ no emit → blank row, never a guessed/0 price) for every
    # shape that can't yield a clean price. The five it must survive:
    # 1) a string that isn't a price body
    assert _monid_price_from_native({"result": "not json"}) is None
    # 2) a content-items list with no price body
    assert _monid_price_from_native({"result": [{"type": "text", "text": "hi"}]}) is None
    # 3) a dict result with no price
    assert _monid_price_from_native({"result": {"other": 1}}) is None
    assert _monid_price_from_native({"result": json.dumps({"price": {}})}) is None
    # 4) an errored prepare — even with a well-formed body
    assert _monid_price_from_native({"result": PRICE_JSON, "is_error": True}) is None
    # 5) an omitted result (dynamicToolCall success with result None)
    assert _monid_price_from_native({"result": None, "result_omitted": True}) is None
    # And the whole native missing / empty text.
    assert _monid_price_from_native(None) is None
    assert _monid_price_from_native({"result": ""}) is None
    # A string / bool / negative micro is not a trustworthy price → None.
    assert _monid_price_from_native(
        {"result": json.dumps({"price": {"unit_price_micro": "10000"}})}
    ) is None
    assert _monid_price_from_native(
        {"result": json.dumps({"price": {"unit_price_micro": True}})}
    ) is None
    assert _monid_price_from_native(
        {"result": json.dumps({"price": {"unit_price_micro": -1}})}
    ) is None


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


def test_projector_emits_price_for_codex_monid_prepare(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime, "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    _drive(projector, _completed("monid_prepare", {"result": PRICE_JSON}))
    assert calls == [
        ("a1", "tool_use", {"tool": "monid_prepare", "unit_price_micro": 10000, "price_type": "PER_CALL"}),
    ]


def test_projector_price_is_idempotent_per_tool_ref(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime, "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    completed = _completed("monid_prepare", {"result": PRICE_JSON})
    # A re-delivered completed frame for the same tool ref must not re-emit.
    _drive(projector, completed, completed)
    assert len(calls) == 1


def test_projector_ignores_non_monid_and_errored(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        local_runtime, "_emit_status",
        lambda agent_id, event, payload: calls.append((agent_id, event, payload)),
    )
    projector = _LegacyStatusProjector("a1")
    _drive(
        projector,
        _completed("read_inbox", {"result": PRICE_JSON}, ref="r1"),
        _completed("monid_prepare", {"result": PRICE_JSON, "is_error": True}, ref="e1"),
        _completed("monid_prepare", {"result": "not json"}, ref="m1"),
        _completed("monid_prepare", {"result": None, "result_omitted": True}, ref="o1"),
    )
    assert calls == []
