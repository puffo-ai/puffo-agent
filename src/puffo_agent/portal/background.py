"""Detach the daemon into a background process with a status-bar icon.

``puffo-agent start --background`` re-spawns the CLI as a *detached*
child running the tray (``start --tray-runner``), so the daemon
outlives the terminal that launched it. POSIX puts the child in a new
session (setsid); Windows uses ``DETACHED_PROCESS`` so it isn't tied to
the console. The child's stdout/stderr go to ``background.log``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from importlib.util import find_spec

from .state import (
    DAEMON_STARTUP_OBSERVATION_SECONDS,
    DaemonStartupState,
    background_log_path,
    is_daemon_alive,
    is_daemon_ready,
    is_daemon_startup_stalled,
    is_pid_alive,
    read_daemon_pid,
)

# Windows process-creation flags (kept as literals so this imports on
# POSIX, where ``subprocess`` doesn't define them).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_GUI_EXTRA_HINT = (
    "puffo-agent start --background requires the desktop [gui] extra "
    "(PySide6). install it with: pip install 'puffo-agent[gui]' or "
    "uv tool install --force 'puffo-agent[gui]'"
)


def tray_runner_command() -> list[str]:
    """Re-invoke this CLI as the detached tray host. ``-m`` avoids
    depending on the ``puffo-agent`` script being on PATH."""
    return [sys.executable, "-m", "puffo_agent.portal.cli", "start", "--tray-runner"]


def headless_runner_command() -> list[str]:
    """Re-invoke the foreground daemon inside a detached process."""
    return [sys.executable, "-m", "puffo_agent.portal.cli", "start"]


def detach_kwargs(log_handle) -> dict:
    """``subprocess.Popen`` kwargs that fully detach the child from this
    terminal, per platform."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
    }
    if os.name == "nt":
        # DETACHED_PROCESS severs the console connection, but Windows children
        # still inherit their parent's Job object by default.  Terminal hosts
        # and managed launchers may configure that job to terminate its whole
        # process tree when the window closes.  Break away as well so
        # ``--background`` has the same lifetime contract as POSIX setsid().
        kwargs["creationflags"] = (
            _DETACHED_PROCESS
            | _CREATE_NEW_PROCESS_GROUP
            | _CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _observe_pid_startup(
    pid: int,
    *,
    timeout: float = DAEMON_STARTUP_OBSERVATION_SECONDS,
    proc=None,
) -> DaemonStartupState:
    deadline = time.monotonic() + timeout
    while True:
        if is_daemon_ready(pid):
            return DaemonStartupState.READY
        if proc is not None:
            if proc.poll() is not None:
                return DaemonStartupState.EXITED
        elif not is_pid_alive(pid):
            return DaemonStartupState.EXITED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return DaemonStartupState.STARTING
        time.sleep(min(0.1, remaining))


def _observe_background_startup(
    proc,
    timeout: float = DAEMON_STARTUP_OBSERVATION_SECONDS,
) -> DaemonStartupState:
    return _observe_pid_startup(proc.pid, timeout=timeout, proc=proc)


def _existing_daemon_result() -> int | None:
    if not is_daemon_alive():
        return None
    pid = read_daemon_pid()
    if pid is None:
        print("puffo-agent daemon has no readable pid.", file=sys.stderr)
        return 1
    if is_daemon_startup_stalled(pid):
        print(
            f"puffo-agent daemon is alive but stalled during startup (pid={pid}).",
            file=sys.stderr,
        )
        print(f"  logs: {background_log_path()}", file=sys.stderr)
        return 1
    startup = _observe_pid_startup(pid)
    if startup is DaemonStartupState.READY:
        print(f"puffo-agent daemon already running (pid={pid}).")
        return 0
    if startup is DaemonStartupState.STARTING:
        print(f"puffo-agent daemon already starting (pid={pid}).")
        print(f"  logs: {background_log_path()}")
        return 0
    print(
        f"puffo-agent daemon exited before becoming ready (pid={pid}).",
        file=sys.stderr,
    )
    return 1


def _spawn_detached(command: list[str], *, tray: bool) -> int:
    log_path = background_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab")
    try:
        proc = subprocess.Popen(command, **detach_kwargs(log_handle))
    finally:
        # The child inherited its own copy of the fd; drop ours.
        log_handle.close()

    startup = _observe_background_startup(proc)
    if startup is DaemonStartupState.EXITED:
        winner_pid = read_daemon_pid()
        if (
            proc.poll() == 0
            and winner_pid is not None
            and is_pid_alive(winner_pid)
        ):
            print(
                "puffo-agent daemon already running or starting "
                f"(pid={winner_pid})."
            )
            print(f"  logs: {log_path}")
            return 0
        print(
            "puffo-agent background process exited before becoming ready; "
            "inspect the log for details.",
            file=sys.stderr,
        )
        print(f"  logs: {log_path}", file=sys.stderr)
        return 1

    if startup is DaemonStartupState.READY:
        print(f"puffo-agent running in the background (pid={proc.pid}).")
    else:
        print(
            "puffo-agent started in the background and is still initializing "
            f"(pid={proc.pid})."
        )
        print("  check readiness with `puffo-agent status`.")
    if tray:
        print(
            "  status-bar icon → Open UI (beta) or Quit "
            "(or run `puffo-agent stop`)."
        )
    else:
        print("  stop with `puffo-agent stop`.")
    print(f"  logs: {log_path}")
    return 0


def spawn_headless_background() -> int:
    """Detach the daemon without Qt, used by one-step machine linking."""
    if (existing := _existing_daemon_result()) is not None:
        return existing
    return _spawn_detached(headless_runner_command(), tray=False)


def spawn_background() -> int:
    """Launch the detached tray+daemon. Returns an exit code for the
    foreground caller, which exits immediately afterward."""
    if (existing := _existing_daemon_result()) is not None:
        return existing
    if find_spec("PySide6") is None:
        print(_GUI_EXTRA_HINT, file=sys.stderr)
        return 1
    return _spawn_detached(tray_runner_command(), tray=True)
