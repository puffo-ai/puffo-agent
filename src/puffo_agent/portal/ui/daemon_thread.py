"""Run ``run_daemon()`` on a non-daemon background thread.

Qt owns the main thread (mac strict). The asyncio daemon lives here.
Stop is routed through the file sentinel so we don't need to refactor
``run_daemon`` to expose the ``Daemon`` instance — the reconcile loop
picks it up within ~2s.

Thread is non-daemon: when the user closes the window, Qt's ``exec()``
returns, but the Python interpreter waits for this thread, which the
daemon's ``os._exit(0)`` ultimately terminates after worker cleanup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

from ..state import read_daemon_pid, write_stop_request

logger = logging.getLogger(__name__)


class DaemonThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="puffo-daemon", daemon=False)
        self._stop_requested = False

    def run(self) -> None:
        from ..daemon import run_daemon
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_daemon())
        except Exception:
            logger.exception("daemon thread crashed")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def request_stop(self) -> bool:
        """Schedule daemon shutdown via the stop sentinel.

        Returns ``True`` when the sentinel was written (caller should
        keep the window up and wait for ``os._exit``). Returns
        ``False`` when we don't own the daemon — caller should close
        immediately because nothing will terminate the process.
        """
        if self._stop_requested:
            return True
        self._stop_requested = True
        owner = read_daemon_pid()
        if owner != os.getpid():
            logger.info(
                "not writing stop sentinel: PID owner=%s, ours=%s",
                owner, os.getpid(),
            )
            return False
        try:
            write_stop_request()
            return True
        except Exception:
            logger.exception("failed to write stop sentinel")
            return False
