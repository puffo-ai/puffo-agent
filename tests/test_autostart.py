"""After-login autostart registration (`puffo-agent autostart`).

All three platform branches run on any host: launchctl/systemctl are
replaced by a recording fake runner, winreg by an in-memory fake, and
HOME / PUFFO_AGENT_HOME point into tmp_path so nothing touches the real
user session.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from puffo_agent.portal import autostart


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / ".puffo-agent"))
    # A host XDG_CONFIG_HOME would point the unit path at the real config.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Path.home() reads HOME on POSIX but USERPROFILE on Windows.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


class FakeRunner:
    """Records commands; per-prefix return codes, default failure for
    `launchctl print` (= not loaded) and success otherwise."""

    def __init__(self, fail_prefixes=(), loaded=False):
        self.calls: list[list[str]] = []
        self.fail_prefixes = [list(p) for p in fail_prefixes]
        self.loaded = loaded

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return _proc(cmd, 0 if self.loaded else 113)
        for prefix in self.fail_prefixes:
            if cmd[: len(prefix)] == prefix:
                return _proc(cmd, 1, stderr="fake failure")
        return _proc(cmd, 0)


def _proc(cmd, code, stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, code, stdout="", stderr=stderr)


# ── definitions embed absolute interpreter + explicit home ───────────────────


def test_launchagent_definition_is_supervised_and_env_explicit(tmp_path):
    content = autostart.launchagent_plist(
        autostart.daemon_command(),
        tmp_path / "autostart.log",
        {"PUFFO_AGENT_HOME": str(tmp_path)},
    )
    data = plistlib.loads(content)
    assert data["ProgramArguments"][0] == sys.executable
    assert data["ProgramArguments"][1:] == ["-m", "puffo_agent.portal.cli", "start"]
    assert data["RunAtLoad"] is True
    # Crash ⇒ relaunch; clean `stop` (and the pid-lock "already
    # running" exit 0) ⇒ stay down.
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["PUFFO_AGENT_HOME"] == str(tmp_path)


def test_systemd_unit_quotes_interpreter_and_restarts_on_failure(tmp_path):
    unit = autostart.systemd_unit(
        ["/opt/some dir/python", "-m", "puffo_agent.portal.cli", "start"],
        {"PUFFO_AGENT_HOME": str(tmp_path)},
    )
    assert 'ExecStart="/opt/some dir/python" -m puffo_agent.portal.cli start' in unit
    assert f"Environment=PUFFO_AGENT_HOME={tmp_path}" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert autostart._unit_interpreter(unit) == "/opt/some dir/python"


# ── macOS ────────────────────────────────────────────────────────────────────


def test_macos_enable_writes_plist_and_bootstraps(monkeypatch):
    # A deliberately dirty PATH: empty entries and a relative one mean
    # "resolve from the cwd" — a planting vector for a daemon whose cwd
    # the user doesn't control. Only absolute entries may persist.
    monkeypatch.setenv("PATH", "/usr/bin::relative/bin:/opt/tools/bin:")
    run = FakeRunner()
    result = autostart.enable_macos(run)
    assert result.ok
    path = autostart.launchagent_path()
    assert path.exists()
    assert ["launchctl", "bootstrap"] == run.calls[-1][:2]
    assert str(path) in run.calls[-1]
    # The wiring, not just the formatter: what enable actually wrote
    # must carry the effective home and the enable-time PATH (generic /
    # ACP runtimes spawn bare commands that skip the CLI resolver).
    env = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
    assert env["PUFFO_AGENT_HOME"] == str(autostart.home_dir())
    assert env["PATH"] == "/usr/bin:/opt/tools/bin"


def test_macos_enable_is_idempotent_when_loaded_and_current():
    autostart.enable_macos(FakeRunner())
    run = FakeRunner(loaded=True)
    result = autostart.enable_macos(run)
    assert result.ok
    assert any("already enabled" in line for line in result.lines)
    assert not any(cmd[:2] == ["launchctl", "bootstrap"] for cmd in run.calls)


def test_macos_enable_reloads_changed_definition_via_bootout():
    autostart.enable_macos(FakeRunner())
    path = autostart.launchagent_path()
    data = plistlib.loads(path.read_bytes())
    data["ProgramArguments"][0] = "/old/venv/python"
    path.write_bytes(plistlib.dumps(data, sort_keys=True))

    run = FakeRunner(loaded=True)
    result = autostart.enable_macos(run)
    assert result.ok
    ops = [cmd[1] for cmd in run.calls if cmd[0] == "launchctl"]
    assert ops.index("bootout") < ops.index("bootstrap")


def test_macos_enable_falls_back_to_load_and_reports_total_failure():
    run = FakeRunner(fail_prefixes=[["launchctl", "bootstrap"]])
    result = autostart.enable_macos(run)
    assert result.ok
    assert ["launchctl", "load", "-w"] == run.calls[-1][:3]

    run = FakeRunner(
        fail_prefixes=[["launchctl", "bootstrap"], ["launchctl", "load"]]
    )
    result = autostart.enable_macos(run)
    assert not result.ok
    # The failure message must be actionable: file location + manual step.
    assert any("launchctl bootstrap" in line for line in result.lines)


def test_macos_disable_removes_and_reports_not_enabled():
    autostart.enable_macos(FakeRunner())
    result = autostart.disable_macos(FakeRunner(loaded=True))
    assert result.ok
    assert not autostart.launchagent_path().exists()

    again = autostart.disable_macos(FakeRunner())
    assert again.ok
    assert any("not enabled" in line for line in again.lines)


def test_macos_status_distinguishes_configured_loaded_and_stale():
    assert autostart.status_macos(FakeRunner()).configured is False

    autostart.enable_macos(FakeRunner())
    state = autostart.status_macos(FakeRunner(loaded=True))
    assert state.configured and state.active

    state = autostart.status_macos(FakeRunner(loaded=False))
    assert state.configured and not state.active

    path = autostart.launchagent_path()
    data = plistlib.loads(path.read_bytes())
    data["ProgramArguments"][0] = "/gone/python"
    path.write_bytes(plistlib.dumps(data, sort_keys=True))
    state = autostart.status_macos(FakeRunner(loaded=True))
    assert not state.configured
    assert any("stale" in line for line in state.lines)


# ── Linux ────────────────────────────────────────────────────────────────────


def test_linux_enable_writes_unit_and_enables_now():
    run = FakeRunner()
    result = autostart.enable_linux(run)
    assert result.ok
    assert autostart.systemd_unit_path().exists()
    assert ["systemctl", "--user", "daemon-reload"] in run.calls
    assert ["systemctl", "--user", "enable", "--now", "puffo-agent.service"] in run.calls
    assert not any(cmd[0] == "loginctl" for cmd in run.calls)
    # As on macOS: the unit enable actually wrote must carry the
    # effective home and the enable-time PATH.
    unit = autostart.systemd_unit_path().read_text()
    assert f"PUFFO_AGENT_HOME={autostart.home_dir()}" in unit
    assert "PATH=" in unit


def test_linux_enable_linger_is_best_effort():
    run = FakeRunner()
    result = autostart.enable_linux(run, linger=True)
    assert result.ok
    assert ["loginctl", "enable-linger"] in run.calls
    assert any("before login" in line for line in result.lines)

    run = FakeRunner(fail_prefixes=[["loginctl"]])
    result = autostart.enable_linux(run, linger=True)
    assert result.ok  # linger failure must not fail the enable
    assert any("login-time only" in line for line in result.lines)


def test_linux_enable_reports_missing_user_session():
    run = FakeRunner(fail_prefixes=[["systemctl"]])
    result = autostart.enable_linux(run)
    assert not result.ok
    assert any("systemd" in line for line in result.lines)


def test_linux_disable_removes_unit_and_reloads():
    autostart.enable_linux(FakeRunner())
    run = FakeRunner()
    result = autostart.disable_linux(run)
    assert result.ok
    assert not autostart.systemd_unit_path().exists()
    assert ["systemctl", "--user", "daemon-reload"] in run.calls


def test_linux_disable_failure_keeps_unit_and_reports():
    """A failed `disable --now` must not delete the unit and claim
    success — that leaves a running service reported as removed."""
    autostart.enable_linux(FakeRunner())
    run = FakeRunner(
        fail_prefixes=[["systemctl", "--user", "disable"]]
    )
    result = autostart.disable_linux(run)
    assert not result.ok
    assert autostart.systemd_unit_path().exists()
    assert any("fake failure" in line for line in result.lines)


def test_linux_disable_surfaces_daemon_reload_failure():
    autostart.enable_linux(FakeRunner())
    run = FakeRunner(
        fail_prefixes=[["systemctl", "--user", "daemon-reload"]]
    )
    result = autostart.disable_linux(run)
    assert not result.ok
    # The unit itself was disabled and removed; the message must say
    # what remains to be done rather than fail opaquely.
    assert not autostart.systemd_unit_path().exists()
    assert any("daemon-reload" in line for line in result.lines)


def test_linux_disable_absent_unit_is_ok_even_without_user_manager():
    run = FakeRunner(fail_prefixes=[["systemctl"]])
    result = autostart.disable_linux(run)
    assert result.ok
    assert any("not enabled" in line for line in result.lines)


def test_systemd_env_assignment_is_quoted_and_specifier_safe(tmp_path):
    unit = autostart.systemd_unit(
        ["/opt/py%thon", "-m", "puffo_agent.portal.cli", "start"],
        {"PUFFO_AGENT_HOME": '/home/A User/di"r\\.puffo-agent'},
    )
    assert (
        'Environment="PUFFO_AGENT_HOME=/home/A User/di\\"r\\\\.puffo-agent"'
        in unit
    )
    # % is a systemd specifier in unit files; it must be doubled in
    # both Environment and ExecStart or systemd expands it.
    assert "py%%thon" in unit
    assert autostart._unit_interpreter(unit) == "/opt/py%thon"


def test_linux_enable_restarts_a_running_service_only_on_change():
    """`enable --now` does not restart an active unit, so a changed
    definition needs an explicit restart — and an unchanged one must
    not bounce the running daemon."""
    restart = ["systemctl", "--user", "restart", "puffo-agent.service"]

    run = FakeRunner()  # is-active → 0: service already running
    result = autostart.enable_linux(run)
    assert result.ok
    # Ordering: the restart must come after daemon-reload re-read the
    # changed unit, or systemd restarts into the old definition.
    assert run.calls.index(["systemctl", "--user", "daemon-reload"]) < run.calls.index(restart)

    run = FakeRunner()
    result = autostart.enable_linux(run)  # second run: definition unchanged
    assert result.ok
    assert restart not in run.calls


def test_linux_disable_stops_a_loaded_service_despite_missing_unit_file():
    """A hand-deleted unit file with the service still running must not
    report "not enabled" over a live daemon."""
    run = FakeRunner()  # is-active → 0: still running
    result = autostart.disable_linux(run)
    assert result.ok
    assert ["systemctl", "--user", "stop", "puffo-agent.service"] in run.calls
    assert any("stopped" in line for line in result.lines)

    run = FakeRunner(fail_prefixes=[["systemctl", "--user", "stop"]])
    result = autostart.disable_linux(run)
    assert not result.ok
    assert any("stop" in line and "failed" in line for line in result.lines)


def test_missing_systemctl_reports_instead_of_tracebacking(monkeypatch):
    def raise_missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", raise_missing)
    result = autostart.enable_linux(autostart._run)
    assert not result.ok
    assert any("command not found" in line for line in result.lines)


def test_systemd_unit_path_honors_absolute_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert autostart.systemd_unit_path() == (
        tmp_path / "xdg" / "systemd" / "user" / "puffo-agent.service"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")  # ignored, like systemd
    assert autostart.systemd_unit_path() == (
        tmp_path / ".config" / "systemd" / "user" / "puffo-agent.service"
    )


def test_linux_status_distinguishes_enabled_active_and_stale():
    autostart.enable_linux(FakeRunner())
    state = autostart.status_linux(FakeRunner())
    assert state.configured and state.active

    state = autostart.status_linux(FakeRunner(fail_prefixes=[["systemctl", "--user", "is-active"]]))
    assert state.configured
    assert any("not currently active" in line for line in state.lines)

    path = autostart.systemd_unit_path()
    path.write_text(path.read_text().replace(sys.executable, "/gone/python"))
    state = autostart.status_linux(FakeRunner())
    assert not state.configured
    assert any("stale" in line for line in state.lines)


# ── Windows ──────────────────────────────────────────────────────────────────


class _FakeKey:
    """Stands in for a winreg handle; the fake module functions below
    receive it back, mirroring the real module-level winreg API."""

    def __init__(self, reg):
        self.reg = reg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}

    def OpenKey(self, hive, path, *args):
        return _FakeKey(self)

    def SetValueEx(self, key, name, reserved, kind, value):
        key.reg.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.reg.values:
            raise FileNotFoundError(name)
        del key.reg.values[name]

    def QueryValueEx(self, key, name):
        if name not in key.reg.values:
            raise FileNotFoundError(name)
        return key.reg.values[name], self.REG_SZ


@pytest.fixture()
def fake_winreg(monkeypatch):
    reg = FakeWinreg()
    monkeypatch.setattr(autostart, "_load_winreg", lambda: reg)
    return reg


def test_windows_enable_registers_quoted_command(monkeypatch, fake_winreg):
    monkeypatch.delenv("PUFFO_AGENT_HOME")
    result = autostart.enable_windows()
    assert result.ok
    value = fake_winreg.values[autostart.WINDOWS_RUN_VALUE]
    assert "puffo_agent.portal.cli" in value and value.endswith("start")
    # Console interpreter without a pythonw sibling ⇒ the console-window
    # caveat must be stated, not hidden.
    assert any("console window" in line for line in result.lines)


def test_windows_enable_refuses_only_a_diverging_home_override(monkeypatch, fake_winreg):
    # A session-only override that login won't see → refuse, actionably.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(Path.home() / "elsewhere"))
    result = autostart.enable_windows()
    assert not result.ok
    assert any("setx" in line for line in result.lines)
    assert autostart.WINDOWS_RUN_VALUE not in fake_winreg.values

    # After the suggested `setx`, the variable still exists in every
    # shell — the persisted value matching the effective one must
    # enable, not repeat the refusal forever.
    fake_winreg.values["PUFFO_AGENT_HOME"] = str(Path.home() / "elsewhere")
    result = autostart.enable_windows()
    assert result.ok
    assert autostart.WINDOWS_RUN_VALUE in fake_winreg.values


def test_windows_enable_accepts_override_equal_to_login_default(fake_winreg):
    # The autouse fixture sets PUFFO_AGENT_HOME to <tmp>/.puffo-agent —
    # present, but identical to what login resolves. "Is the variable
    # set" would refuse this; only actual divergence may.
    result = autostart.enable_windows()
    assert result.ok
    assert autostart.WINDOWS_RUN_VALUE in fake_winreg.values


def test_windows_prefers_pythonw_sibling(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.touch()
    (tmp_path / "pythonw.exe").touch()
    monkeypatch.setattr(autostart.sys, "executable", str(python))
    command = autostart.windows_run_command()
    assert command[0].endswith("pythonw.exe")


def test_windows_disable_and_status(monkeypatch, fake_winreg):
    monkeypatch.delenv("PUFFO_AGENT_HOME")
    assert autostart.status_windows().configured is False

    autostart.enable_windows()
    state = autostart.status_windows()
    assert state.configured
    assert any("unsupervised" in line for line in state.lines)

    fake_winreg.values[autostart.WINDOWS_RUN_VALUE] = '"C:\\gone\\python.exe" -m x start'
    state = autostart.status_windows()
    assert not state.configured
    assert any("stale" in line for line in state.lines)

    result = autostart.disable_windows()
    assert result.ok
    assert autostart.WINDOWS_RUN_VALUE not in fake_winreg.values
    again = autostart.disable_windows()
    assert again.ok
    assert any("not enabled" in line for line in again.lines)


# ── CLI wiring ───────────────────────────────────────────────────────────────


def test_cli_autostart_status_prints_lines(monkeypatch, capsys):
    from puffo_agent.portal import cli

    monkeypatch.setattr(
        autostart,
        "status",
        lambda **_: autostart.AutostartStatus(True, True, ["configured: X", "loaded"]),
    )
    assert cli.main(["autostart", "status"]) == 0
    out = capsys.readouterr().out
    assert "configured: X" in out and "loaded" in out


def test_cli_autostart_enable_failure_exits_nonzero(monkeypatch, capsys):
    from puffo_agent.portal import cli

    monkeypatch.setattr(
        autostart,
        "enable",
        lambda **_: autostart.ActionResult(False, ["it broke, do Y"]),
    )
    assert cli.main(["autostart", "enable"]) == 1
    assert "it broke" in capsys.readouterr().err
