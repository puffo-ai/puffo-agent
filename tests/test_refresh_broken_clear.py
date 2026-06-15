"""PUF-303: ``Worker._clear_refresh_broken_if_recoverable`` worker-
side reactive clear, fired from the daemon's on_refresh_success
callback so manual ``claude auth login`` recovery doesn't have to
wait for the next CredentialRefresher poll-tick.

Mirrors ``_clear_auth_failed_if_recoverable``.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import Worker


def _make_runtime(health: str) -> RuntimeState:
    rt = RuntimeState(status="running", started_at=0, msg_count=0)
    rt.health = health
    rt.error = "stale message"
    return rt


def test_clear_only_when_refresh_broken(tmp_path, monkeypatch):
    """The clear is gated on ``runtime.health == "refresh_broken"``.
    No-op for any other state so we don't accidentally clobber a
    legitimate auth_failed / api_error_abandoned / in_progress flag."""
    monkeypatch.chdir(tmp_path)
    for non_broken_health in (
        "ok", "auth_failed", "api_error_abandoned",
        "in_progress", "unhandled_error",
    ):
        rt = _make_runtime(non_broken_health)
        Worker._clear_refresh_broken_if_recoverable(
            rt, "t-agent", logging.getLogger("test"),
        )
        assert rt.health == non_broken_health
        assert rt.error == "stale message"


def test_clear_flips_to_ok(tmp_path, monkeypatch):
    """Happy path: refresh_broken → ok + error cleared."""
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("refresh_broken")
    Worker._clear_refresh_broken_if_recoverable(
        rt, "t-agent", logging.getLogger("test"),
    )
    assert rt.health == "ok"
    assert rt.error == ""


def test_clear_logs_recovery(tmp_path, monkeypatch, caplog):
    """The clear emits an info log naming refresh_broken → ok so the
    operator can grep for recoveries when debugging."""
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("refresh_broken")
    log = logging.getLogger("puffo_agent.portal.worker.test")
    with caplog.at_level(logging.INFO):
        Worker._clear_refresh_broken_if_recoverable(rt, "t-agent", log)
    assert any(
        "refresh_broken back to ok" in r.message for r in caplog.records
    )


def test_clear_no_op_when_already_ok(tmp_path, monkeypatch, caplog):
    """Idempotency: clear from ok stays ok, no log, no save spam."""
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("ok")
    rt.error = ""  # match real ok state
    log = logging.getLogger("puffo_agent.portal.worker.test")
    with caplog.at_level(logging.INFO):
        Worker._clear_refresh_broken_if_recoverable(rt, "t-agent", log)
    assert rt.health == "ok"
    assert not any(
        "refresh_broken" in r.message for r in caplog.records
    )
