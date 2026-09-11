"""Login-time autostart registration for the puffo-agent daemon.

``puffo-agent autostart enable`` registers the daemon to start
automatically after the user logs in; ``disable`` removes the
registration; ``status`` reports both what is configured on disk and
what the platform's service manager currently has loaded — the two can
diverge (file present but never loaded, interpreter upgraded away, ...)
and conflating them would report an autostart that will not fire.

This is deliberately per-user, login-session autostart — not a
pre-login system service. Credentials the daemon needs (Keychain
entries, ``~/.pi``, OpenCode auth) live in the user session, so a
system-level service would start into an environment where every
harness reports signed-out. On Linux, ``--linger`` optionally upgrades
to boot-time start via ``loginctl enable-linger`` (best-effort; not all
systems permit it).

Platform mechanisms:

- macOS: a LaunchAgent plist. launchd supervises the foreground
  ``start`` directly; ``KeepAlive.SuccessfulExit=false`` restarts the
  daemon after a crash but not after a clean ``puffo-agent stop`` (and
  not after the pid-lock "already running" no-op, which exits 0).
- Linux: a systemd user unit with ``Restart=on-failure``.
- Windows: an ``HKCU`` Run key. Windows offers no supervision on this
  path — the daemon starts at login and is not restarted on crash;
  ``status`` says so rather than implying launchd/systemd semantics.

Every registration embeds the absolute interpreter path
(``sys.executable -m puffo_agent.portal.cli start`` — the repo's
established PATH-free re-invocation) and the effective
``PUFFO_AGENT_HOME``, because login-time environments have neither the
user's shell PATH nor its exports.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .state import home_dir

LAUNCHD_LABEL = "ai.puffo.agent"
WINDOWS_RUN_VALUE = "PuffoAgent"
_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SYSTEMD_UNIT_NAME = "puffo-agent.service"


def daemon_command() -> list[str]:
    """The exact argv a service manager should supervise."""
    return [sys.executable, "-m", "puffo_agent.portal.cli", "start"]


def autostart_log_path() -> Path:
    return home_dir() / "logs" / "autostart.log"


def _service_env() -> dict[str, str]:
    # Always explicit: a login-time environment does not inherit the
    # shell export the user enabled from, and "defaulted at enable time,
    # different default at boot" is exactly the class of silent
    # divergence this feature must not introduce.
    env = {"PUFFO_AGENT_HOME": str(home_dir())}
    # Login services get a minimal PATH. Known harness CLIs ride
    # cli_bin's broader-than-PATH resolver, but generic/ACP runtime
    # configs spawn bare commands (``opencode acp``, ...) exactly as
    # written — persist the enable-time PATH so those still resolve.
    # Absolute entries only: an empty or relative entry resolves from
    # the cwd, which an interactive shell controls but a daemon doesn't.
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and os.path.isabs(entry)
    ]
    if entries:
        env["PATH"] = os.pathsep.join(entries)
    return env


# ── result / status types ────────────────────────────────────────────────────


@dataclass
class ActionResult:
    ok: bool
    lines: list[str] = field(default_factory=list)


@dataclass
class AutostartStatus:
    """``configured`` is what's on disk; ``active`` is what the service
    manager currently has loaded. Both false ⇒ disabled."""

    configured: bool
    active: bool
    lines: list[str] = field(default_factory=list)


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        # No launchctl/systemctl on this box (e.g. a non-systemd distro):
        # report it like any failed call instead of tracebacking.
        return subprocess.CompletedProcess(
            cmd, 127, stdout="", stderr=f"{cmd[0]}: command not found"
        )


def _failure_detail(proc: subprocess.CompletedProcess) -> str:
    detail = (proc.stderr or proc.stdout or "").strip()
    return detail.splitlines()[0] if detail else f"exit code {proc.returncode}"


# ── macOS (launchd LaunchAgent) ──────────────────────────────────────────────


def launchagent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchagent_plist(
    command: list[str], log_path: Path, env: dict[str, str]
) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "EnvironmentVariables": env,
        },
        sort_keys=True,
    )


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_loaded(run: Runner) -> bool:
    return run(["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"]).returncode == 0


def enable_macos(run: Runner = _run) -> ActionResult:
    path = launchagent_path()
    log_path = autostart_log_path()
    content = launchagent_plist(daemon_command(), log_path, _service_env())
    if path.exists() and path.read_bytes() == content and _launchd_loaded(run):
        return ActionResult(True, ["already enabled (LaunchAgent loaded, definition current)"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    lines = [f"wrote {path}"]
    if _launchd_loaded(run):
        # Definition changed while loaded: launchd only re-reads a plist
        # across bootout/bootstrap. This restarts a launchd-run daemon.
        run(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        lines.append("reloading changed LaunchAgent definition")
    proc = run(["launchctl", "bootstrap", _launchd_domain(), str(path)])
    if proc.returncode != 0:
        # Older macOS has no bootstrap subcommand.
        proc = run(["launchctl", "load", "-w", str(path)])
    if proc.returncode != 0:
        lines.append(
            "launchctl could not load the LaunchAgent: "
            f"{_failure_detail(proc)}. The file is in place; it will "
            "load at next login, or run `launchctl bootstrap "
            f"{_launchd_domain()} {path}` manually."
        )
        return ActionResult(False, lines)
    lines.append("daemon will start automatically after login (supervised by launchd)")
    return ActionResult(True, lines)


def disable_macos(run: Runner = _run) -> ActionResult:
    path = launchagent_path()
    lines: list[str] = []
    if _launchd_loaded(run):
        proc = run(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        if proc.returncode != 0:
            return ActionResult(
                False,
                [f"launchctl bootout failed: {_failure_detail(proc)}; LaunchAgent left in place"],
            )
        lines.append("unloaded LaunchAgent (a launchd-run daemon is stopped by this)")
    if path.exists():
        path.unlink()
        lines.append(f"removed {path}")
    if not lines:
        lines.append("autostart was not enabled")
    return ActionResult(True, lines)


def status_macos(run: Runner = _run) -> AutostartStatus:
    path = launchagent_path()
    if not path.exists():
        return AutostartStatus(False, False, ["disabled (no LaunchAgent)"])
    lines = [f"configured: {path}"]
    configured = True
    try:
        interpreter = plistlib.loads(path.read_bytes())["ProgramArguments"][0]
        if not Path(interpreter).exists():
            configured = False
            lines.append(
                f"stale: interpreter {interpreter} no longer exists "
                "(re-run `puffo-agent autostart enable` from the current install)"
            )
    except Exception:
        configured = False
        lines.append("stale: plist unreadable; re-run `puffo-agent autostart enable`")
    active = _launchd_loaded(run)
    lines.append(
        "loaded in launchd (will start after login)"
        if active
        else "not loaded in launchd (takes effect at next login, or enable again)"
    )
    return AutostartStatus(configured, active, lines)


# ── Linux (systemd user unit) ────────────────────────────────────────────────


def systemd_unit_path() -> Path:
    # systemd reads user units from $XDG_CONFIG_HOME/systemd/user and,
    # like systemd itself, a non-absolute XDG_CONFIG_HOME is ignored.
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"
    return base / "systemd" / "user" / SYSTEMD_UNIT_NAME


def systemd_unit(command: list[str], env: dict[str, str]) -> str:
    exec_start = " ".join(_systemd_quote(part) for part in command)
    # The whole NAME=value assignment is one quoted token: an unquoted
    # value with whitespace gets split into separate (broken)
    # assignments by systemd.
    env_lines = "".join(
        f"Environment={_systemd_quote(f'{name}={value}')}\n"
        for name, value in sorted(env.items())
    )
    return (
        "[Unit]\n"
        "Description=Puffo.ai agent daemon\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        f"{env_lines}"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_quote(part: str) -> str:
    # % first: it is a systemd specifier everywhere in a unit file
    # (ExecStart and Environment alike) and would otherwise expand.
    part = part.replace("%", "%%")
    if not part or any(ch.isspace() or ch in '"\\' for ch in part):
        escaped = part.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return part


def enable_linux(run: Runner = _run, *, linger: bool = False) -> ActionResult:
    path = systemd_unit_path()
    content = systemd_unit(daemon_command(), _service_env())
    changed = not (path.exists() and path.read_text() == content)
    # Queried before `enable --now` can start anything: an already-running
    # service keeps executing the old definition until restarted below.
    was_active = (
        changed
        and run(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME]).returncode == 0
    )
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    lines = [f"wrote {path}" if changed else f"unit unchanged at {path}"]
    commands: list[list[str]] = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
    ]
    if was_active:
        # `enable --now` only starts an inactive unit — the launchd
        # branch's bootout/bootstrap equivalent for a changed definition.
        commands.append(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME])
        lines.append("restarting the running service to apply the changed definition")
    for cmd in commands:
        proc = run(cmd)
        if proc.returncode != 0:
            lines.append(
                f"`{' '.join(cmd)}` failed: {_failure_detail(proc)}. "
                "The unit file is in place; on systems without a systemd "
                "user session, autostart is not available this way."
            )
            return ActionResult(False, lines)
    lines.append("daemon will start automatically after login (supervised by systemd)")
    if linger:
        proc = run(["loginctl", "enable-linger"])
        lines.append(
            "lingering enabled: the daemon also starts at boot, before login"
            if proc.returncode == 0
            else f"could not enable lingering ({_failure_detail(proc)}); "
            "autostart remains login-time only"
        )
    return ActionResult(True, lines)


def disable_linux(run: Runner = _run) -> ActionResult:
    path = systemd_unit_path()
    if not path.exists():
        # The unit file can be gone while systemd still runs the service
        # (deleted by hand, an earlier config root, ...). "Not enabled"
        # over a live daemon would be a lie — stop it. A failing
        # is-active (including: no user manager at all) means nothing is
        # running and nothing is left behind to report.
        if run(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME]).returncode != 0:
            return ActionResult(True, ["autostart was not enabled"])
        proc = run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])
        if proc.returncode != 0:
            return ActionResult(
                False,
                [
                    f"no unit file at {path}, but the service is running and "
                    f"`systemctl --user stop` failed: {_failure_detail(proc)}"
                ],
            )
        return ActionResult(
            True,
            [f"no unit file at {path}; stopped the still-loaded service"],
        )
    proc = run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    if proc.returncode != 0:
        # Deleting the unit anyway would orphan a still-running /
        # still-wanted service while reporting success.
        return ActionResult(
            False,
            [
                f"`systemctl --user disable --now` failed: {_failure_detail(proc)}; "
                f"unit left in place at {path}"
            ],
        )
    lines = ["disabled systemd unit (a systemd-run daemon is stopped by this)"]
    path.unlink()
    lines.append(f"removed {path}")
    proc = run(["systemctl", "--user", "daemon-reload"])
    if proc.returncode != 0:
        lines.append(
            f"`systemctl --user daemon-reload` failed: {_failure_detail(proc)}. "
            "The unit is disabled and its file removed; run the reload "
            "manually so systemd forgets the stale definition."
        )
        return ActionResult(False, lines)
    return ActionResult(True, lines)


def status_linux(run: Runner = _run) -> AutostartStatus:
    path = systemd_unit_path()
    if not path.exists():
        return AutostartStatus(False, False, ["disabled (no systemd user unit)"])
    lines = [f"configured: {path}"]
    configured = True
    interpreter = _unit_interpreter(path.read_text())
    if interpreter is not None and not Path(interpreter).exists():
        configured = False
        lines.append(
            f"stale: interpreter {interpreter} no longer exists "
            "(re-run `puffo-agent autostart enable` from the current install)"
        )
    enabled = run(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME]).returncode == 0
    running = run(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME]).returncode == 0
    lines.append(
        "enabled in systemd (will start after login)"
        if enabled
        else "unit file present but not enabled in systemd"
    )
    lines.append("service currently active" if running else "service not currently active")
    return AutostartStatus(configured, enabled, lines)


def _unit_interpreter(unit_text: str) -> str | None:
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            rest = line[len("ExecStart=") :].strip()
            try:
                # _systemd_quote emits shell-compatible double-quoting,
                # so shlex round-trips it (spaces, escaped quotes); the
                # %-specifier doubling is ours to undo.
                return shlex.split(rest)[0].replace("%%", "%")
            except (ValueError, IndexError):
                return None
    return None


# ── Windows (HKCU Run key) ───────────────────────────────────────────────────


def _load_winreg():
    import winreg

    return winreg


def windows_run_command() -> list[str]:
    """Prefer the venv's ``pythonw.exe`` so login doesn't flash (or
    leave) a console window; fall back to the console interpreter."""
    command = daemon_command()
    interpreter = Path(command[0])
    windowless = interpreter.with_name("pythonw.exe")
    if interpreter.name.lower() == "python.exe" and windowless.exists():
        command[0] = str(windowless)
    return command


def _windows_login_home() -> Path:
    """The PUFFO_AGENT_HOME a login-started daemon will see: the
    persisted per-user value if any (where ``setx`` writes), else the
    default. Comparing against the current effective home — not against
    "is the variable set" — is what keeps ``setx`` + re-run from being
    refused forever."""
    winreg = _load_winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "PUFFO_AGENT_HOME")
    except (FileNotFoundError, OSError):
        value = ""
    if str(value):
        return Path(str(value)).expanduser()
    return Path.home() / ".puffo-agent"


def enable_windows() -> ActionResult:
    login_home = _windows_login_home()
    if login_home != home_dir():
        # Run-key entries carry no per-entry environment; enabling under
        # a session-only override would silently start the daemon
        # against a different home at next login. Refuse rather than
        # diverge.
        return ActionResult(
            False,
            [
                f"PUFFO_AGENT_HOME resolves to {home_dir()} here but would "
                f"resolve to {login_home} at login; a Run-key entry cannot "
                "carry the override. Persist it first (`setx "
                "PUFFO_AGENT_HOME ...`) or unset it, then re-run "
                "`puffo-agent autostart enable`."
            ],
        )
    winreg = _load_winreg()
    command = windows_run_command()
    value = subprocess.list2cmdline(command)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, value)
    lines = [f"registered Run entry: {value}"]
    if Path(command[0]).name.lower() != "pythonw.exe":
        lines.append("note: no pythonw.exe next to the interpreter; a console window will open at login")
    lines.append(
        "daemon will start automatically after login "
        "(Windows Run entries are unsupervised: no restart after a crash)"
    )
    return ActionResult(True, lines)


def disable_windows() -> ActionResult:
    winreg = _load_winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return ActionResult(True, ["autostart was not enabled"])
    return ActionResult(True, ["removed Run entry"])


def status_windows() -> AutostartStatus:
    winreg = _load_winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return AutostartStatus(False, False, ["disabled (no Run entry)"])
    lines = [f"configured: Run entry {value!r}"]
    configured = True
    interpreter = value.split('"')[1] if value.startswith('"') else value.split(" ", 1)[0]
    if not Path(interpreter).exists():
        configured = False
        lines.append(
            f"stale: interpreter {interpreter} no longer exists "
            "(re-run `puffo-agent autostart enable` from the current install)"
        )
    # A Run entry has no loaded/unloaded state distinct from the file:
    # it fires at every login while present.
    lines.append(
        "will start after login (unsupervised: no restart after a crash)"
    )
    return AutostartStatus(configured, configured, lines)


# ── platform dispatch ────────────────────────────────────────────────────────


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def enable(*, linger: bool = False, platform: str | None = None) -> ActionResult:
    platform = platform or _platform()
    if platform == "macos":
        return enable_macos()
    if platform == "windows":
        return enable_windows()
    return enable_linux(linger=linger)


def disable(*, platform: str | None = None) -> ActionResult:
    platform = platform or _platform()
    if platform == "macos":
        return disable_macos()
    if platform == "windows":
        return disable_windows()
    return disable_linux()


def status(*, platform: str | None = None) -> AutostartStatus:
    platform = platform or _platform()
    if platform == "macos":
        return status_macos()
    if platform == "windows":
        return status_windows()
    return status_linux()
