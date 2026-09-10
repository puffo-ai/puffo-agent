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

import asyncio
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


# ── Re-arm cadence after a no-progress turn ──────────────────────────────
# The guard above names the turn; this half bounds how fast it repeats.
# ``_wake_remaining_pending`` re-arms at ZERO delay whenever rows stay pending,
# so a turn that can never admit its batch re-runs as fast as it can fail. On
# staging 2026-09-09 one agent behind a capped LLM gateway turned that into 354
# rejected requests in two minutes (~1 turn/s, 3 gateway calls each) until the
# streak guard cancelled it. (PUF-382)


class _Coalescer:
    def __init__(self):
        self.delays: list[float] = []

    def notify(self, *, delay_seconds=None):
        self.delays.append(delay_seconds)


class _Store:
    def __init__(self, *, pending=True, candidates=True):
        self._pending, self._candidates = pending, candidates

    async def get_pending(self, limit=1):
        return [object()] if self._pending else []

    async def get_notice_candidates(self, session_id):
        return [object()] if self._candidates else []


def _rearm_runtime(**store_kw):
    """A runtime with only what ``_wake_remaining_pending`` touches."""
    rt = GlobalInboxRuntime.__new__(GlobalInboxRuntime)
    rt._init_recovery_gates(None)
    rt.store = _Store(**store_kw)
    rt.adapter = SimpleNamespace(get_provider_session_id=lambda: "session")
    rt.coalescer = _Coalescer()
    rt.notified = 0

    def _notify():
        rt.notified += 1

    rt.notify = _notify
    return rt


def test_a_single_no_progress_turn_still_re_arms_immediately():
    """One deferral is legitimate — today's immediate follow-up is kept, so
    the common case is not slowed by the bound below."""
    rt = _rearm_runtime()
    rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    assert (rt.notified, rt.coalescer.delays) == (1, [])


def test_repeated_no_progress_backs_off_instead_of_spinning():
    """The second and later re-arms go through the coalescer with a delay.
    Without this the loop is bounded only by how fast the provider fails."""
    rt = _rearm_runtime()
    for _ in range(2):
        rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    assert rt.coalescer.delays == [5.0]
    assert rt.notified == 0, "notify() would pin the delay back to zero"

    rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    assert rt.coalescer.delays == [5.0, 10.0, 20.0]


def test_the_backoff_is_bounded():
    rt = _rearm_runtime()
    for _ in range(40):
        rt.note_no_progress_turn()
    assert rt.next_no_progress_rearm_delay() == 300.0


def test_a_turn_that_admits_its_batch_clears_the_backoff():
    rt = _rearm_runtime()
    for _ in range(5):
        rt.note_no_progress_turn()
    assert rt.next_no_progress_rearm_delay() > 0.0
    rt._clear_no_progress_rearm_backoff()  # what a `succeeded` settle does
    rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    assert (rt.notified, rt.coalescer.delays) == (1, [])


def test_nothing_re_arms_when_there_is_no_pending_work():
    """The bound must not invent a wake: an empty pending set or no notice
    candidate still re-arms nothing at all."""
    for kw in ({"pending": False}, {"candidates": False}):
        rt = _rearm_runtime(**kw)
        for _ in range(3):
            rt.note_no_progress_turn()
        asyncio.run(rt._wake_remaining_pending())
        assert (rt.notified, rt.coalescer.delays) == (0, []), kw


def test_a_degraded_runtime_still_owns_its_own_backoff():
    rt = _rearm_runtime()
    rt._degraded = True
    for _ in range(3):
        rt.note_no_progress_turn()
    asyncio.run(rt._wake_remaining_pending())
    assert (rt.notified, rt.coalescer.delays) == (0, [])


@pytest.mark.asyncio
async def test_real_ingress_cuts_through_a_backed_off_re_arm():
    """A message arriving mid-backoff must not wait it out. The runtime's own
    re-arm is a deadline like any other, and the coalescer only ever lets a
    deadline move EARLIER — the same guarantee proved for the degraded backoff
    in ``test_coalescer_pulls_a_pending_long_deadline_into_the_normal_window``.
    """
    from puffo_agent.agent.inbox_scheduler import InboxCoalescer

    now = 100.0
    sleeps: list[float] = []
    entered, release = asyncio.Event(), asyncio.Event()

    def monotonic():
        return now

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        entered.set()
        await release.wait()
        now += delay

    coalescer = InboxCoalescer(sleep=sleep, monotonic=monotonic)
    rt = _rearm_runtime()
    rt.coalescer = coalescer
    for _ in range(6):  # attempts 1..6 → 0, 5, 10, 20, 40, 80
        rt.note_no_progress_turn()
    await rt._wake_remaining_pending()
    assert coalescer._deadlines[0] == pytest.approx(now + 80.0)

    waiter = asyncio.create_task(coalescer.wait_for_burst())
    await asyncio.wait_for(entered.wait(), timeout=1)
    entered.clear()
    coalescer.notify(delay_seconds=0.0)  # a message arrives
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert sleeps[0] == pytest.approx(80.0) and sleeps[1] == pytest.approx(0.0)
    release.set()
    await asyncio.wait_for(waiter, timeout=1)


def test_the_escalation_ladder_is_explicit():
    """Pinned by value: 0 s for the first, then doubling from 5 s to a 300 s
    ceiling. A silent change here changes how long a wedged agent sleeps."""
    rt = _rearm_runtime()
    ladder = []
    for _ in range(9):
        rt.note_no_progress_turn()
        ladder.append(rt.next_no_progress_rearm_delay())
    assert ladder == [0.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]


# ── The loop itself ──────────────────────────────────────────────────────
# The tests above pin the helpers. This one drives the real ``process_once``
# so the *wiring* is covered too: deleting the counter call or the reset in
# the settle block must fail a test, not just look wrong in review.


def _loop_runtime(outcomes):
    """A runtime whose turns settle as ``outcomes`` says, with the real
    ``process_once`` / ``_wake_remaining_pending`` bodies and everything else
    stubbed to the shortest thing that lets the turn reach its settle."""
    from puffo_agent.agent.global_inbox_types import RuntimeHealth

    rt = _rearm_runtime()
    rt.health = RuntimeHealth()
    rt._boundary = asyncio.Lock()
    rt._turn_state_lock = asyncio.Lock()
    rt._autonomous_settle_pending = None
    rt._autonomous_turn_id = ""
    rt.process_outcome = None
    rt.attempts = SimpleNamespace(reset=lambda: None)
    rt.active = SimpleNamespace(turn_id="turn-1", provider_session_id="s", provider_turn_id="t")
    rt.adapter = SimpleNamespace(
        get_provider_session_id=lambda: "session",
        register_admission_callback=lambda *a, **k: None,
    )
    planned = SimpleNamespace(
        turn_id="turn-1", notice_message_ids=("m1",), targets=(), notice_generation=0,
        planning_cycle_key="cycle",
    )
    settled = iter(outcomes)

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    rt._replay_deferred_autonomous_start = _noop
    rt.plan_pending = lambda: _immediate(planned)
    rt._resolve_context_plan = lambda p: _immediate(p)
    rt._notice_is_current = _true
    rt._start_notice_unless_autonomous = _true
    rt._invoke_turn_with_retries = _noop
    rt._health_outcome_for_turn = lambda p: next(settled)
    rt._mark_active_processed = _noop
    rt._notify_status_terminal = _noop
    rt._finalize_process = lambda *a, **k: None
    return rt


def _immediate(value):
    async def _run():
        return value
    return _run()


def test_a_wedged_turn_stops_spinning_after_the_first_repeat():
    """The storm, reproduced: every turn settles ``no_progress`` and the rows
    stay pending. Before the bound, each pass re-armed at zero delay and the
    loop ran as fast as the provider could fail."""
    rt = _loop_runtime(["no_progress"] * 4)
    for _ in range(4):
        assert asyncio.run(rt.process_once()) is True
    assert rt.notified == 1, "only the first repeat re-arms immediately"
    assert rt.coalescer.delays == [5.0, 10.0, 20.0]


def test_a_turn_that_makes_progress_clears_the_bound():
    """A recovered provider must not stay throttled: the next wedged turn
    starts the ladder again from immediate."""
    rt = _loop_runtime(["no_progress", "no_progress", "succeeded", "no_progress"])
    for _ in range(4):
        asyncio.run(rt.process_once())
    assert rt.coalescer.delays == [5.0], "one backed-off re-arm, before the success"
    assert rt.notified == 3, "first repeat, the success, and the fresh streak"
