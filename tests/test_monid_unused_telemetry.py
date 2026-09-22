"""Unit tests for the weak-model paid-data telemetry probe (_MonidUnusedProbe).

The probe logs one line per model turn that ran at least one tool but none of
the monid paid-data tools, so a model under-using paid data is measurable. A
turn with no tool call at all is deliberately silent (indistinguishable from
chit-chat at this layer).
"""

import logging

from puffo_agent.agent.harness.driver import HarnessEvent, SessionRef, TurnRef
from puffo_agent.agent.harness.runtime.local_runtime import _MonidUnusedProbe

_LOGGER = "puffo_agent.agent.harness.runtime.local_runtime"
_MARK = "none monid"  # substring unique to the telemetry line


def _ev(kind: str, turn: str = "t1", *, label=None, ref=None) -> HarnessEvent:
    data: dict = {}
    if label is not None:
        data["label"] = label
    if ref is not None:
        data["tool_call_ref"] = ref
    return HarnessEvent.normalized(
        type=kind, driver="codex",
        session_ref=SessionRef("s1"), turn_ref=TurnRef(turn), data=data,
    )


def _run(probe: _MonidUnusedProbe, events) -> None:
    for event in events:
        probe.observe(event)


def _hits(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _MARK in r.getMessage()]


def test_logs_when_tools_ran_but_no_monid(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started"),
            _ev("turn.tool_started", label="read_inbox", ref="c1"),
            _ev("turn.tool_started", label="whoami", ref="c2"),
            _ev("turn.completed"),
        ])
    hits = _hits(caplog)
    assert len(hits) == 1
    assert "agent_1" in hits[0]
    assert "2 tool call(s)" in hits[0]


def test_no_log_when_a_monid_tool_was_called(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started"),
            _ev("turn.tool_started", label="read_inbox", ref="c1"),
            _ev("turn.tool_started", label="monid_spend", ref="c2"),
            _ev("turn.completed"),
        ])
    assert _hits(caplog) == []


def test_scoped_monid_label_is_recognized(caplog):
    # The real event carries the mcp-scoped label; it must normalize to the
    # bare monid name and suppress the log.
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started"),
            _ev("turn.tool_started", label="mcp__puffo__monid_prepare", ref="c1"),
            _ev("turn.completed"),
        ])
    assert _hits(caplog) == []


def test_no_log_when_turn_had_no_tools(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [_ev("turn.started"), _ev("turn.completed")])
    assert _hits(caplog) == []


def test_abandoned_turn_also_fires(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started"),
            _ev("turn.tool_started", label="read_inbox", ref="c1"),
            _ev("turn.abandoned"),
        ])
    assert len(_hits(caplog)) == 1


def test_state_resets_between_turns(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started", "t1"),
            _ev("turn.tool_started", "t1", label="read_inbox", ref="c1"),
            _ev("turn.completed", "t1"),
            _ev("turn.started", "t2"),
            _ev("turn.tool_started", "t2", label="monid_spend", ref="c2"),
            _ev("turn.completed", "t2"),
        ])
    # Only the first turn (tools, no monid) logs; the second is suppressed.
    assert len(_hits(caplog)) == 1


def test_duplicate_tool_ref_counts_once(caplog):
    probe = _MonidUnusedProbe("agent_1")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _run(probe, [
            _ev("turn.started"),
            _ev("turn.tool_started", label="read_inbox", ref="c1"),
            _ev("turn.tool_started", label="read_inbox", ref="c1"),
            _ev("turn.completed"),
        ])
    hits = _hits(caplog)
    assert len(hits) == 1
    assert "1 tool call(s)" in hits[0]
