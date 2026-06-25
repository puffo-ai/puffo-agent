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


class DaemonThread(threading.Thread):
    def __init__(self, with_local_bridge: bool = False) -> None:
        super().__init__(name="puffo-daemon", daemon=False)
        self._stop_requested = False
        self._with_local_bridge = with_local_bridge

    def run(self) -> None:
        from ..daemon import run_daemon
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_daemon(with_local_bridge=self._with_local_bridge))
        except Exception:
            logger.exception("daemon thread crashed")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def request_stop(self) -> bool:
        """Write the stop sentinel iff we own the daemon. Returns True
        when written (caller should wait for ``os._exit``), False when
        another daemon owns the PID file."""
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
