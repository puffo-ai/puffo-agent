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
    background_log_path,
    is_daemon_alive,
    is_daemon_ready,
    is_pid_alive,
    read_daemon_pid,
)

# Windows process-creation flags (kept as literals so this imports on
# POSIX, where ``subprocess`` doesn't define them).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_STARTUP_TIMEOUT_SECONDS = 10.0
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
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _wait_for_pid_ready(
    pid: int,
    *,
    timeout: float = _STARTUP_TIMEOUT_SECONDS,
    proc=None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_daemon_ready(pid):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        if not is_pid_alive(pid):
            return False
        time.sleep(0.1)
    return is_daemon_ready(pid)


def _wait_for_background_ready(proc, timeout: float = _STARTUP_TIMEOUT_SECONDS) -> bool:
    return _wait_for_pid_ready(proc.pid, timeout=timeout, proc=proc)


def _stop_failed_background(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def _existing_daemon_result() -> int | None:
    if not is_daemon_alive():
        return None
    pid = read_daemon_pid()
    if pid is not None and _wait_for_pid_ready(pid):
        print(f"puffo-agent daemon already running (pid={pid}).")
        return 0
    print(
        f"puffo-agent daemon startup failed to become ready (pid={pid}).",
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

    if not _wait_for_background_ready(proc):
        _stop_failed_background(proc)
        print(
            "puffo-agent background startup failed; inspect the log for details.",
            file=sys.stderr,
        )
        print(f"  logs: {log_path}", file=sys.stderr)
        return 1

    print(f"puffo-agent running in the background (pid={proc.pid}).")
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
