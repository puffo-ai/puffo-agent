"""A turn that woke on an announced batch and read none of it must not
settle the agent back to healthy.

Regression cover for the Pi credential-expiry incident: an expired provider
token produced ``stopReason: error`` with zero output, the harness driver
mapped that onto "assistant completed", and every wake-up then settled
``runtime.health = ok`` while the announced messages stayed unread — for five
days, with 19 messages queued behind a green status dot.

The guard is deliberately driver-independent. Fixing the driver that mis-maps
its errors leaves every other driver exempt; this reads the runtime's own
admission bookkeeping instead, so a driver that swallows an error cannot
suppress it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.portal.worker import Worker

LOG = logging.getLogger("no-progress-test")


class _Runtime:
    def __init__(self, health="ok", error=""):
        self.health = health
        self.error = error
        self.saved = []

    def save(self, agent_id):
        self.saved.append((agent_id, self.health))


def _planned(notice_ids):
    return SimpleNamespace(notice_message_ids=tuple(notice_ids))


def _runtime_with(admitted):
    return SimpleNamespace(active=SimpleNamespace(message_ids=list(admitted)))


# ── which turns count as progress ──────────────────────────────────────────


def test_notice_announcing_nothing_is_always_a_success():
    """Autonomous / notice-only wake-ups have no batch to make progress on."""
    outcome = GlobalInboxRuntime._health_outcome_for_turn(
        _runtime_with([]), _planned([])
    )
    assert outcome == "succeeded"


def test_reading_the_batch_is_progress_even_with_no_reply():
    """Deciding not to reply happens after read_inbox admits the rows, so a
    deliberately silent turn still records progress."""
    outcome = GlobalInboxRuntime._health_outcome_for_turn(
        _runtime_with(["m1"]), _planned(["m1"])
    )
    assert outcome == "succeeded"


def test_announced_batch_left_untouched_is_not_a_success():
    outcome = GlobalInboxRuntime._health_outcome_for_turn(
        _runtime_with([]), _planned(["m1", "m2"])
    )
    assert outcome == "no_progress"


def test_mid_turn_arrivals_cannot_trip_the_check():
    """The comparison is against the ids this turn's notice carried, not the
    queue depth afterwards — otherwise a message arriving mid-turn would make
    a perfectly good turn look stalled."""
    outcome = GlobalInboxRuntime._health_outcome_for_turn(
        _runtime_with(["m1"]), _planned(["m1"])
    )
    assert outcome == "succeeded"


# ── what the worker does with a streak of them ─────────────────────────────


def _worker(health="ok", streak=0):
    return SimpleNamespace(runtime=_Runtime(health=health), _no_progress_turns=streak)


def test_one_no_progress_turn_holds_health_and_writes_nothing():
    """A single deferral is legitimate; only a run of them is the signal."""
    w = _worker()
    Worker._note_no_progress_turn(w, "agent-a")

    assert w._no_progress_turns == 1
    assert w.runtime.health == "ok"
    assert w.runtime.saved == []


def test_streak_reaching_the_threshold_turns_the_agent_red():
    w = _worker()
    for _ in range(3):
        Worker._note_no_progress_turn(w, "agent-a")

    assert w.runtime.health == "no_progress"
    assert "read none of them" in w.runtime.error
    assert w.runtime.saved == [("agent-a", "no_progress")]


def test_further_no_progress_turns_do_not_rewrite_the_same_state():
    w = _worker()
    for _ in range(5):
        Worker._note_no_progress_turn(w, "agent-a")

    # Still red, but the streak keeps counting into the operator message.
    assert w.runtime.health == "no_progress"
    assert "5 times in a row" in w.runtime.error


def test_a_named_cause_stays_authoritative():
    """auth_failed / drained / … already say why; a no-progress streak is the
    weaker, derived signal and must not overwrite them."""
    w = _worker(health="auth_failed")
    w.runtime.error = "original reason"
    for _ in range(4):
        Worker._note_no_progress_turn(w, "agent-a")

    assert w.runtime.health == "auth_failed"
    assert w.runtime.error == "original reason"
    assert w.runtime.saved == []


def test_in_progress_and_unknown_are_claimable():
    for start in ("in_progress", "unknown"):
        w = _worker(health=start)
        for _ in range(3):
            Worker._note_no_progress_turn(w, "agent-a")
        assert w.runtime.health == "no_progress", start


# ── the operator-visible surface ───────────────────────────────────────────


def _agent_list_output(monkeypatch, capsys, health):
    """Run ``puffo-cli agent list`` against one stub agent and return its row."""
    import argparse
    import time

    from puffo_agent.portal import cli

    runtime = SimpleNamespace(
        status="running",
        updated_at=int(time.time()),
        started_at=int(time.time()) - 60,
        msg_count=3,
        health=health,
    )
    monkeypatch.setattr(cli, "discover_agents", lambda: ["agent-a"])
    monkeypatch.setattr(cli, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(
        cli.AgentConfig, "load", staticmethod(
            lambda _aid: SimpleNamespace(display_name="Tester", state="enabled")
        )
    )
    monkeypatch.setattr(
        cli.RuntimeState, "load", staticmethod(lambda _aid: runtime)
    )
    cli.cmd_agent_list(argparse.Namespace())
    return [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("agent-a")
    ][0]


def test_no_progress_is_visible_in_agent_list(monkeypatch, capsys):
    """The whole point of the guard is that an operator can see it. The one
    surface they read first must not silently drop the new value."""
    assert "[no_progress]" in _agent_list_output(monkeypatch, capsys, "no_progress")


def test_every_non_ok_health_value_is_visible(monkeypatch, capsys):
    """Stated over the declared enum rather than a copy of it: a value added
    to ``RuntimeState.health`` later is shown by construction, instead of
    being exempt until someone remembers this list."""
    for health in (
        "in_progress", "auth_failed", "api_error_abandoned", "provider_error",
        "refresh_broken", "drained", "unhandled_error", "codex_thread_wedged",
        "server_unreachable", "no_progress",
    ):
        row = _agent_list_output(monkeypatch, capsys, health)
        assert f"[{health}]" in row, health


def test_ok_and_unknown_stay_unannotated(monkeypatch, capsys):
    """Only states that carry a call to action are annotated — otherwise the
    marker stops meaning "needs attention"."""
    for health in ("ok", "unknown", ""):
        row = _agent_list_output(monkeypatch, capsys, health)
        assert "[" not in row, health


# ── cancel is not recovery evidence (found by Peter on cdf9a8c) ─────────────


def test_cancel_after_flip_reasserts_a_live_no_progress_streak(tmp_path, monkeypatch):
    """no_progress → _flip_health_in_progress → cancelled must land back on
    no_progress, not launder through the success resolver into ok."""
    from puffo_agent.portal.worker_run import StandardWorkerRun

    rt = _Runtime(health="no_progress", error="red")
    w = SimpleNamespace(runtime=rt, _no_progress_turns=3)
    Worker._flip_health_in_progress(rt, "agent-a", logging.getLogger("t"))
    assert rt.health == "in_progress"  # the batch-top override Peter traced
    StandardWorkerRun._settle_process_health(w, "agent-a", "cancelled", None)
    assert rt.health == "no_progress"
    assert "read none of them" in rt.error


@pytest.mark.parametrize(
    "streak, health_after_turn, expected",
    [
        # below threshold: the pre-existing cancel resolution stands
        (1, "in_progress", "ok"),
        # a cause named during the turn stays authoritative
        (3, "auth_failed", "auth_failed"),
    ],
)
def test_cancel_leaves_other_resolutions_alone(streak, health_after_turn, expected):
    from puffo_agent.portal.worker_run import StandardWorkerRun

    rt = _Runtime(health=health_after_turn, error="")
    w = SimpleNamespace(runtime=rt, _no_progress_turns=streak)
    StandardWorkerRun._settle_process_health(w, "agent-b", "cancelled", None)
    assert rt.health == expected
