"""``start --background`` detach logic + CLI routing.

The Qt tray (``run_tray``) needs a display, so it isn't exercised here;
these cover the detach command, the per-platform Popen flags, the
already-running short-circuit, and that ``cmd_start`` routes modes
to the right entry point.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


from puffo_agent.portal import background as bg
from puffo_agent.portal.state import DaemonStartupState


def test_detached_runner_commands_use_dash_m():
    assert bg.tray_runner_command() == [
        sys.executable, "-m", "puffo_agent.portal.cli", "start", "--tray-runner",
    ]
    assert bg.headless_runner_command() == [
        sys.executable, "-m", "puffo_agent.portal.cli", "start",
    ]


def test_detach_kwargs_posix(monkeypatch):
    monkeypatch.setattr(bg.os, "name", "posix")
    kwargs = bg.detach_kwargs(log_handle="LOG")
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == "LOG" and kwargs["stderr"] == "LOG"


def test_detach_kwargs_windows(monkeypatch):
    """Closing a Windows terminal must not terminate its background daemon."""
    monkeypatch.setattr(bg.os, "name", "nt")
    kwargs = bg.detach_kwargs(log_handle="LOG")
    assert kwargs["creationflags"] == (
        bg._DETACHED_PROCESS
        | bg._CREATE_NEW_PROCESS_GROUP
        | bg._CREATE_BREAKAWAY_FROM_JOB
    )
    assert "start_new_session" not in kwargs


def test_spawn_background_short_circuits_when_already_running(monkeypatch, capsys):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(bg, "read_daemon_pid", lambda: 4321)
    waited = []
    monkeypatch.setattr(
        bg,
        "_observe_pid_startup",
        lambda pid: waited.append(pid) or DaemonStartupState.READY,
    )

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn when a daemon is already running")

    monkeypatch.setattr(bg.subprocess, "Popen", _no_spawn)
    assert bg.spawn_background() == 0
    assert waited == [4321]
    assert "already running (pid=4321)" in capsys.readouterr().out


def test_spawn_background_accepts_existing_daemon_still_starting(monkeypatch, capsys):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(bg, "read_daemon_pid", lambda: 4321)
    monkeypatch.setattr(
        bg,
        "_observe_pid_startup",
        lambda _pid: DaemonStartupState.STARTING,
    )
    monkeypatch.setattr(bg, "is_daemon_startup_stalled", lambda _pid: False)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("must not spawn over an existing daemon process")

    monkeypatch.setattr(bg.subprocess, "Popen", _no_spawn)
    assert bg.spawn_background() == 0
    assert "already starting (pid=4321)" in capsys.readouterr().out


def test_headless_background_reports_existing_daemon_stalled(monkeypatch, capsys):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(bg, "read_daemon_pid", lambda: 4321)
    monkeypatch.setattr(
        bg,
        "_observe_pid_startup",
        lambda _pid: DaemonStartupState.STARTING,
    )
    monkeypatch.setattr(bg, "is_daemon_startup_stalled", lambda _pid: True)

    assert bg.spawn_headless_background() == 1
    assert "stalled during startup" in capsys.readouterr().err


def test_spawn_background_preflights_gui_before_detach(monkeypatch, capsys):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(bg, "find_spec", lambda _name: None)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("must not detach without the GUI dependency")

    monkeypatch.setattr(bg.subprocess, "Popen", _no_spawn)
    assert bg.spawn_background() == 1
    assert "puffo-agent[gui]" in capsys.readouterr().err


def test_spawn_background_detaches_child(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(bg, "find_spec", lambda _name: object())
    monkeypatch.setattr(bg, "background_log_path", lambda: tmp_path / "background.log")
    monkeypatch.setattr(
        bg,
        "_observe_background_startup",
        lambda _proc: DaemonStartupState.READY,
    )

    captured = {}

    class _FakeProc:
        pid = 9999

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(bg.subprocess, "Popen", _fake_popen)
    rc = bg.spawn_background()
    assert rc == 0
    assert captured["cmd"] == bg.tray_runner_command()
    # The detach knobs are present (exact flag set per platform tested above).
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert (tmp_path / "background.log").exists()
    out = capsys.readouterr().out
    assert "running in the background (pid=9999)" in out

    assert bg.spawn_headless_background() == 0
    assert captured["cmd"] == bg.headless_runner_command()


def test_spawn_background_keeps_a_live_child_after_observation_timeout(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(bg, "find_spec", lambda _name: object())
    monkeypatch.setattr(bg, "background_log_path", lambda: tmp_path / "background.log")
    monkeypatch.setattr(
        bg,
        "_observe_background_startup",
        lambda _proc: DaemonStartupState.STARTING,
    )

    class _SlowProc:
        pid = 9999
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 1

    proc = _SlowProc()
    monkeypatch.setattr(bg.subprocess, "Popen", lambda *_a, **_kw: proc)

    assert bg.spawn_background() == 0
    assert proc.terminated is False
    assert "still initializing" in capsys.readouterr().out


def test_observation_prioritizes_ready_then_detects_exit_or_timeout(monkeypatch):
    ready = True
    monkeypatch.setattr(bg, "is_daemon_ready", lambda _pid: ready)

    class _Proc:
        pid = 9999

        def poll(self):
            return 0

    proc = _Proc()
    assert bg._observe_background_startup(proc, timeout=0) is DaemonStartupState.READY

    ready = False
    assert bg._observe_background_startup(proc, timeout=0) is DaemonStartupState.EXITED

    proc.poll = lambda: None

    assert (
        bg._observe_background_startup(proc, timeout=0)
        is DaemonStartupState.STARTING
    )


def test_spawn_background_reports_child_exit_before_ready(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(bg, "find_spec", lambda _name: object())
    monkeypatch.setattr(bg, "background_log_path", lambda: tmp_path / "background.log")
    monkeypatch.setattr(
        bg,
        "_observe_background_startup",
        lambda _proc: DaemonStartupState.EXITED,
    )

    class _ExitedProc:
        pid = 9999

        def poll(self):
            return 1

    monkeypatch.setattr(bg.subprocess, "Popen", lambda *_a, **_kw: _ExitedProc())

    assert bg.spawn_background() == 1
    assert "exited before becoming ready" in capsys.readouterr().err


def test_spawn_background_accepts_redundant_child_exit(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(bg, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(bg, "read_daemon_pid", lambda: 4321)
    monkeypatch.setattr(bg, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bg, "background_log_path", lambda: tmp_path / "background.log")
    monkeypatch.setattr(
        bg,
        "_observe_background_startup",
        lambda _proc: DaemonStartupState.EXITED,
    )

    class _RedundantProc:
        pid = 9999

        def poll(self):
            return 0

    monkeypatch.setattr(bg.subprocess, "Popen", lambda *_a, **_kw: _RedundantProc())
    monkeypatch.setattr(bg, "_existing_daemon_result", lambda: None)

    assert bg.spawn_headless_background() == 0
    assert "already running or starting (pid=4321)" in capsys.readouterr().out


def test_cmd_status_reports_live_unready_daemon_as_starting(monkeypatch, capsys):
    from puffo_agent.portal import cli

    monkeypatch.setattr(cli, "read_daemon_pid", lambda: 4321)
    monkeypatch.setattr(cli, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(cli, "is_daemon_ready", lambda _pid: False)
    monkeypatch.setattr(cli, "is_daemon_startup_stalled", lambda _pid: False)
    monkeypatch.setattr(cli, "stop_requested_for", lambda _pid: False)
    monkeypatch.setattr(cli, "discover_agents", lambda: [])

    assert cli.cmd_status(argparse.Namespace()) == 0
    assert "daemon: starting (pid=4321)" in capsys.readouterr().out


def test_cmd_status_reports_stop_request(monkeypatch, capsys):
    from puffo_agent.portal import cli

    monkeypatch.setattr(cli, "read_daemon_pid", lambda: 4321)
    monkeypatch.setattr(cli, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(cli, "is_daemon_ready", lambda _pid: False)
    monkeypatch.setattr(cli, "stop_requested_for", lambda _pid: True)
    stalled = False
    monkeypatch.setattr(cli, "is_daemon_stop_stalled", lambda _pid: stalled)
    monkeypatch.setattr(cli, "discover_agents", lambda: [])

    assert cli.cmd_status(argparse.Namespace()) == 0
    assert "daemon: stopping (pid=4321)" in capsys.readouterr().out

    stalled = True
    assert cli.cmd_status(argparse.Namespace()) == 0
    assert "daemon: stop stalled (pid=4321)" in capsys.readouterr().out


# ── cmd_start routing ─────────────────────────────────────────────────────────


def _ns(**kw) -> argparse.Namespace:
    base = {
        "ui": False,
        "background": False,
        "detach": False,
        "tray_runner": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_start_routes_tray_runner(monkeypatch):
    import puffo_agent.portal.ui.tray as tray
    monkeypatch.setattr(tray, "run_tray", lambda **kw: 7)
    from puffo_agent.portal.cli import cmd_start
    assert cmd_start(_ns(tray_runner=True)) == 7


def test_cmd_start_routes_background(monkeypatch):
    monkeypatch.setattr(bg, "spawn_background", lambda **kw: 5)
    from puffo_agent.portal.cli import cmd_start
    assert cmd_start(_ns(background=True)) == 5


def test_cmd_start_routes_headless_detach(monkeypatch):
    monkeypatch.setattr(bg, "spawn_headless_background", lambda **kw: 6)
    from puffo_agent.portal.cli import cmd_start

    assert cmd_start(_ns(detach=True)) == 6


def test_cmd_start_tray_runner_takes_priority_over_background(monkeypatch):
    import puffo_agent.portal.ui.tray as tray
    monkeypatch.setattr(tray, "run_tray", lambda **kw: 1)
    monkeypatch.setattr(bg, "spawn_background", lambda **kw: 2)
    from puffo_agent.portal.cli import cmd_start
    assert cmd_start(_ns(tray_runner=True, background=True)) == 1


def test_cmd_start_routes_ui(monkeypatch):
    import puffo_agent.portal.ui.launcher as launcher

    monkeypatch.setattr(launcher, "launch", lambda: 4)
    from puffo_agent.portal.cli import cmd_start

    assert cmd_start(_ns(ui=True)) == 4


def test_cmd_start_routes_foreground_daemon(monkeypatch):
    from puffo_agent.portal import cli, daemon

    async def fake_run_daemon():
        return 3

    monkeypatch.setattr(daemon, "run_daemon", fake_run_daemon)
    assert cli.cmd_start(_ns()) == 3
