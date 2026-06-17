"""Off-main-thread stop-signal gating.

Under ``--ui`` / ``--background`` the daemon runs in a child thread
(DaemonThread) while Qt owns the main thread. ``add_signal_handler`` →
``signal.set_wakeup_fd`` raises ``RuntimeError`` off the main thread, so
the POSIX install must be skipped (the file sentinel stops those modes),
not crash.
"""

from __future__ import annotations

import asyncio
import threading

from puffo_agent.portal.daemon import _install_posix_stop_handlers


def test_posix_stop_handlers_skipped_off_main_thread():
    out: dict = {}

    def run():
        loop = asyncio.new_event_loop()
        try:
            out["installed"] = _install_posix_stop_handlers(loop, lambda: None)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = repr(exc)
        finally:
            loop.close()

    t = threading.Thread(target=run)
    t.start()
    t.join()

    # No RuntimeError from set_wakeup_fd, and nothing installed off-thread.
    assert "error" not in out, out.get("error")
    assert out["installed"] is False
