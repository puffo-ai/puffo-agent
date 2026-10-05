"""Persisted runtime markers must identify an instance, not a reusable PID."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from puffo_agent.portal import background, daemon_identity, state


@pytest.fixture
def process(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    attributes = SimpleNamespace(birth=100.0, home=str(tmp_path))
    proc = SimpleNamespace(
        cmdline=lambda: ["python", "-m", "puffo_agent.portal.cli", "start"],
        create_time=lambda: attributes.birth,
        environ=lambda: {"PUFFO_AGENT_HOME": attributes.home},
        cwd=lambda: str(tmp_path),
    )
    monkeypatch.setattr(daemon_identity.psutil, "Process", lambda pid: proc)
    return attributes


@pytest.mark.parametrize("replacement", ["new_birth", "other_home"])
def test_stale_legacy_markers_do_not_suppress_start(process, tmp_path, replacement):
    """A matching-command process must not inherit another daemon's markers."""
    for path in (state.daemon_pid_path(), state.daemon_ready_path()):
        path.write_text("4242")
        os.utime(path, (200, 200))
    if replacement == "new_birth":
        process.birth = 300.0
    else:
        process.home = str(tmp_path / "another-home")
    assert background._existing_daemon_result() is None
    assert not state.is_daemon_ready(4242)


def test_live_legacy_daemon_can_be_stopped_during_upgrade(process):
    """New CLI must retain stop/readiness support for a running old daemon."""
    for path in (state.daemon_pid_path(), state.daemon_ready_path()):
        path.write_text("4242")
    assert state.is_daemon_ready(4242)
    state.write_stop_request(4242)
    assert state.stop_requested_for(4242)


@pytest.mark.parametrize("replacement", ["new_birth", "other_home", "corrupt"])
def test_modern_identity_is_not_replaced_by_pid_only_fallback(
    process, tmp_path, replacement
):
    """Even valid-looking PID and ready files cannot bypass an invalid identity."""
    state.write_daemon_pid(4242)
    state.write_daemon_ready(4242)
    assert state.is_daemon_ready(4242)
    if replacement == "new_birth":
        process.birth += 1
    elif replacement == "other_home":
        process.home = str(tmp_path / "other")
    else:
        daemon_identity.identity_path(tmp_path).write_text("{}")
    assert not state.is_daemon_alive()
    assert not state.is_daemon_ready(4242)


def test_restart_rejects_old_ready_stop_and_cleanup(process, tmp_path):
    """Restarting a daemon in one process still creates a distinct instance."""
    state.write_daemon_pid(4242)
    state.write_daemon_ready(4242)
    original = state.read_daemon_identity(4242)
    state.write_stop_request(4242, identity=original)
    state.write_daemon_pid(4242)
    successor = state.read_daemon_identity(4242)
    assert original != successor
    assert not state.is_daemon_ready(4242)
    assert not state.stop_requested_for(4242)
    assert not state.is_pid_alive(4242, identity=original)
    state.write_daemon_ready(4242, identity=successor)
    state.write_stop_request(4242, identity=successor)
    assert not state.stop_requested_for(4242, identity=original)
    assert not state.clear_daemon_pid(4242, identity=original)
    assert not state.clear_daemon_ready(4242, identity=original)
    assert not state.clear_stop_request(4242, identity=original)
    assert state.is_daemon_ready(4242)
    assert state.stop_requested_for(4242, identity=successor)


def test_old_cli_stop_must_postdate_current_instance(process):
    """Support old CLI PID-only requests without replaying a pre-start sentinel."""
    state.write_daemon_pid(4242)
    state.stop_request_path().write_text('{"pid": 4242}')
    os.utime(state.stop_request_path(), (1, 1))
    assert not state.stop_requested_for(4242)
    state.stop_request_path().write_text('{"pid": 4242}')
    assert state.stop_requested_for(4242)


def test_inaccessible_process_does_not_authorize_second_daemon(monkeypatch, process):
    """Unverifiable ownership must not be treated as a dead daemon."""
    state.daemon_pid_path().write_text("4242")

    def denied(pid):
        raise daemon_identity.psutil.AccessDenied(pid)

    monkeypatch.setattr(daemon_identity.psutil, "Process", denied)
    with pytest.raises(RuntimeError, match="cannot verify daemon ownership"):
        background._existing_daemon_result()


def test_stop_poll_does_not_wait_for_reused_pid(monkeypatch, process, capsys):
    """A replacement daemon with the same PID must not prolong stop polling."""
    from argparse import Namespace
    from puffo_agent.portal import cli

    state.write_daemon_pid(4242)
    state.write_daemon_ready(4242)
    write = state.write_stop_request

    def replace_after_request(pid, *, identity):
        write(pid, identity=identity)
        process.birth += 1

    monkeypatch.setattr(cli, "write_stop_request", replace_after_request)
    monkeypatch.setattr(
        cli.time, "sleep", lambda _: pytest.fail("waited for reused PID")
    )
    assert cli.cmd_stop(Namespace(timeout=1)) == 0
    assert "daemon stopped" in capsys.readouterr().out


def test_copied_identity_does_not_claim_another_home(monkeypatch, process, tmp_path):
    """An identity file copied into a different home cannot establish ownership."""
    state.write_daemon_pid(4242)
    other = tmp_path / "copy"
    other.mkdir()
    for name in ("daemon.pid", "daemon.identity.json"):
        (other / name).write_bytes((tmp_path / name).read_bytes())
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(other))
    process.home = str(other)
    assert not state.is_daemon_alive()
