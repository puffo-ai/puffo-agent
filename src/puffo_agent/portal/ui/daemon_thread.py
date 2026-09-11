"""Background thread hosting ``run_daemon()`` so Qt can own the main thread.

Stop is routed through the file sentinel — the reconcile loop picks
it up within ~2s. Kept non-daemon so Python waits for the daemon's
``os._exit(0)`` after worker teardown."""
from __future__ import annotations

import asyncio
import logging
import os
import threading

from ..state import read_daemon_pid, write_stop_request

logger = logging.getLogger(__name__)


def install_daemon_watchdog(app, daemon_thread: DaemonThread):
    """Exit the Qt application when its in-process daemon terminates."""
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(100)

    def _poll() -> None:
        if daemon_thread.is_alive():
            return
        timer.stop()
        app.exit(daemon_thread.exit_code or (1 if daemon_thread.failed else 0))

    timer.timeout.connect(_poll)
    timer.start()
    return timer


class DaemonThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="puffo-daemon", daemon=False)
        self._stop_requested = threading.Event()
        self.exit_code: int | None = None
        self.startup_error: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.startup_error is not None or self.exit_code not in (None, 0)

    def run(self) -> None:
        from ..daemon import run_daemon
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.exit_code = loop.run_until_complete(
                run_daemon(self._stop_requested)
            )
        except BaseException as exc:
            self.startup_error = exc
            self.exit_code = 1
            logger.exception("daemon thread crashed")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def request_stop(self) -> bool:
        """Request stop only when this live thread can own the daemon."""
        if self._stop_requested.is_set():
            return True
        if not self.is_alive():
            return False
        owner = read_daemon_pid()
        if owner not in (None, os.getpid()):
            logger.info(
                "not writing stop sentinel: PID owner=%s, ours=%s",
                owner, os.getpid(),
            )
            return False
        # This in-process event reaches run_daemon even when startup has not
        # published its PID yet, or the sentinel write itself fails.
        self._stop_requested.set()
        if owner is None:
            return True
        try:
            write_stop_request(owner)
            return True
        except Exception:
            logger.exception("failed to write stop sentinel")
            return True
