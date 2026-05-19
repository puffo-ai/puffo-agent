"""PUF-207: Worker._run gates on adapter.auth_healthy before warm().

The full Worker._run() path is integration-heavy (HTTP client, keystore,
adapter, MCP). These tests cover the smaller surface the wiring sits
on: the _check_startup_auth_or_pause helper, which is the load-bearing
piece — auth=False → pause with a recoverable message; auth=None /
True → proceed unchanged.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import _check_startup_auth_or_pause


class _FakeAdapter:
    """Stand-in for an Adapter that just carries ``auth_healthy``.
    The helper only reads that one field; we don't need a real
    refresh_ping or run_turn for these tests."""

    def __init__(self, auth_healthy):
        self.auth_healthy = auth_healthy


def test_check_startup_auth_pauses_on_auth_healthy_false(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=False)

    proceed = _check_startup_auth_or_pause(adapter, runtime, "test-agent-001")

    assert proceed is False
    assert runtime.status == "paused"
    assert runtime.health == "auth_failed"
    # Recovery prompt must give the user every concrete step they
    # need; the FB-159 silent-fail mode was largely a misattribution
    # of "my python is broken" because the message wasn't actionable.
    assert "Claude Code OAuth" in runtime.error
    assert "claude" in runtime.error and "/login" in runtime.error
    assert "agent resume test-agent-001" in runtime.error
    assert "Terminal" in runtime.error  # separate-shell hint
    assert "puffo-agent" in runtime.error  # full-path hint surface
    # Per Equation, the agent id must appear in the displayed prose,
    # not only inside the command template. Count occurrences so a
    # future "trim duplicate id" refactor doesn't silently break the
    # ask.
    assert runtime.error.count("test-agent-001") >= 2


def test_check_startup_auth_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=False)

    _check_startup_auth_or_pause(adapter, runtime, "agent-persist")

    reloaded = RuntimeState.load("agent-persist")
    assert reloaded is not None
    assert reloaded.status == "paused"
    assert reloaded.health == "auth_failed"
    assert "Claude Code OAuth" in reloaded.error


def test_check_startup_auth_proceeds_when_probe_skipped(tmp_path, monkeypatch):
    """sdk / chat-only adapters short-circuit refresh_ping and leave
    auth_healthy at the default ``None``. The helper must not pause
    them — they have no credential TTL to probe."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=None)

    proceed = _check_startup_auth_or_pause(adapter, runtime, "test-agent-002")

    assert proceed is True
    # Runtime untouched — no save, no field changes.
    assert runtime.status == "running"
    assert runtime.health == "unknown"
    assert runtime.error == ""


def test_check_startup_auth_proceeds_when_probe_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=True)

    proceed = _check_startup_auth_or_pause(adapter, runtime, "test-agent-003")

    assert proceed is True
    assert runtime.status == "running"
    assert runtime.health == "unknown"
    assert runtime.error == ""


def test_check_startup_auth_not_sticky(tmp_path, monkeypatch):
    """The startup gate must not lock the agent in auth_failed
    forever. After auto-pause, a subsequent probe-success (via the
    existing periodic tick at worker.py:705-728) should be able to
    recover health=ok and the operator can resume. This is a smoke
    test of that contract — the gate doesn't mutate any state that
    the tick can't reverse."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=False)

    _check_startup_auth_or_pause(adapter, runtime, "test-agent-004")
    assert runtime.health == "auth_failed"

    # Simulate the periodic tick observing a successful refresh.
    adapter.auth_healthy = True
    runtime.health = "ok"
    runtime.save("test-agent-004")
    reloaded = RuntimeState.load("test-agent-004")
    assert reloaded is not None
    assert reloaded.health == "ok"


# ── Call-site contract: pause path must set warm_done before return ──
#
# Reviewer ask: a future refactor that drops the `self._warm_done.set()`
# line on the pause branch would hang any caller awaiting
# `wait_for_warm()`. Mirror the exact production shape from `Worker._run()`
# so a regression there breaks this test before it breaks prod — same
# pattern that landed in PUF-214's `_fallback_call_site` helper.


async def _startup_call_site(
    adapter, runtime, agent_id, warm_done, *, warm_called,
):
    """Mirrors the production block in ``Worker._run()``:

        if not _check_startup_auth_or_pause(self._adapter, ...):
            self._warm_done.set()
            return
        await self._adapter.warm(...)  # normal path
        ...
        finally:
            self._warm_done.set()

    Returns ``"paused"`` if the gate auto-paused, ``"warmed"`` if it
    proceeded. ``warm_called`` is mutated in-place when warm() would
    have fired."""
    if not _check_startup_auth_or_pause(adapter, runtime, agent_id):
        warm_done.set()
        return "paused"
    # Normal warm path — finally-block guarantees the gate releases.
    try:
        warm_called.append(True)
    finally:
        warm_done.set()
    return "warmed"


def test_call_site_releases_warm_gate_on_pause(tmp_path, monkeypatch):
    """Load-bearing: pause-path MUST set warm_done so wait_for_warm()
    callers don't hang. Catches a future refactor that drops the
    `self._warm_done.set()` line before the early return."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=False)
    warm_done = asyncio.Event()
    warm_called: list[bool] = []

    result = asyncio.run(_startup_call_site(
        adapter, runtime, "agent-pause-warm", warm_done,
        warm_called=warm_called,
    ))

    assert result == "paused"
    # Warm gate released even though warm() never ran — that's the
    # whole point of the early-return + set pattern.
    assert warm_done.is_set() is True
    assert warm_called == []  # warm() did NOT fire on pause path
    # And the operator-side surface still got populated.
    assert runtime.status == "paused"


def test_call_site_releases_warm_gate_on_proceed(tmp_path, monkeypatch):
    """Sanity: when the gate lets the agent proceed, warm() fires
    and the gate still releases (via the finally block)."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    adapter = _FakeAdapter(auth_healthy=True)
    warm_done = asyncio.Event()
    warm_called: list[bool] = []

    result = asyncio.run(_startup_call_site(
        adapter, runtime, "agent-proceed-warm", warm_done,
        warm_called=warm_called,
    ))

    assert result == "warmed"
    assert warm_done.is_set() is True
    assert warm_called == [True]
    assert runtime.status == "running"
