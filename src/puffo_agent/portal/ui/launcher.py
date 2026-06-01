"""Entry point for ``puffo-agent start`` in UI mode.

Hosts the asyncio daemon on a background thread and runs Qt on the
main thread. Closing the window writes the stop sentinel; the daemon
finishes worker cleanup and calls ``os._exit(0)``, terminating the
whole process.
"""
from __future__ import annotations

import logging
import sys

from .daemon_thread import DaemonThread
from .log_buffer import install_log_buffer


def launch() -> int:
    # basicConfig first: install_log_buffer adds a handler, which makes
    # basicConfig a no-op (root keeps its default WARNING level and
    # drops every INFO record the daemon emits).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log_buffer = install_log_buffer(maxlen=500)

    from PySide6.QtWidgets import QApplication
    from .main_window import MainWindow

    daemon_thread = DaemonThread()
    daemon_thread.start()

    app = QApplication(sys.argv)
    app.setApplicationName("Puffo Agent")
    app.setQuitOnLastWindowClosed(True)

    window = MainWindow(daemon_thread=daemon_thread, log_buffer=log_buffer)
    window.show()

    return app.exec()
