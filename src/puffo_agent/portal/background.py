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

from .state import background_log_path, is_daemon_alive, read_daemon_pid

# Windows process-creation flags (kept as literals so this imports on
# POSIX, where ``subprocess`` doesn't define them).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def tray_runner_command(with_local_bridge: bool = False) -> list[str]:
    """Re-invoke this CLI as the detached tray host. ``-m`` avoids
    depending on the ``puffo-agent`` script being on PATH."""
    cmd = [sys.executable, "-m", "puffo_agent.portal.cli", "start", "--tray-runner"]
    if with_local_bridge:
        cmd.append("--with-local-bridge")
    return cmd


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


def spawn_background(with_local_bridge: bool = False) -> int:
    """Launch the detached tray+daemon. Returns an exit code for the
    foreground caller, which exits immediately afterward."""
    if is_daemon_alive():
        print(f"puffo-agent daemon already running (pid={read_daemon_pid()}).")
        return 0

    log_path = background_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            tray_runner_command(with_local_bridge), **detach_kwargs(log_handle)
        )
    finally:
        # The child inherited its own copy of the fd; drop ours.
        log_handle.close()

    print(f"puffo-agent running in the background (pid={proc.pid}).")
    print("  status-bar icon → Open UI (beta) or Quit (or run `puffo-agent stop`).")
    print(f"  logs: {log_path}")
    return 0
