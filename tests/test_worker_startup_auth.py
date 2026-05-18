"""PUF-207: Worker._run gates on adapter.auth_healthy before warm().

The full Worker._run() path is integration-heavy (HTTP client, keystore,
adapter, MCP). These tests cover the smaller surface the wiring sits
on: the _check_startup_auth_or_pause helper, which is the load-bearing
piece — auth=False → pause with a recoverable message; auth=None /
True → proceed unchanged.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import _check_startup_auth_or_pause


class _FakeAdapter:
    """Stand-in for an Adapter that just carries ``auth_healthy``.
    The helper only reads that one field; we don't need a real
    refresh_ping or run_turn for these tests."""

    def __init__(self, auth_healthy):
        self.auth_healthy = auth_healthy


def _agent_dir_for(tmp_path: Path, agent_id: str) -> Path:
    """``RuntimeState.save`` writes to ``runtime_json_path(agent_id)``
    which resolves through ``state.agent_dir`` → ``$PUFFO_HOME``.
    Tests point ``PUFFO_HOME`` at a tmp dir so save() doesn't escape
    the sandbox."""
    return tmp_path


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
