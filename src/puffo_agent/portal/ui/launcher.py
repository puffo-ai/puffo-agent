"""Entry point for ``puffo-agent start --ui``."""
from __future__ import annotations

import logging
import sys

from .daemon_thread import DaemonThread
from .log_buffer import install_log_buffer


def launch() -> int:
    # basicConfig must run before install_log_buffer attaches a handler,
    # otherwise root stays at WARNING and INFO records drop.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log_buffer = install_log_buffer(maxlen=500)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from .assets import logo_path
    from .main_window import MainWindow
    from .style import APP_STYLESHEET

    daemon_thread = DaemonThread()
    daemon_thread.start()

    app = QApplication(sys.argv)
    app.setApplicationName("Puffo Agent")
    app.setWindowIcon(QIcon(str(logo_path())))
    app.setQuitOnLastWindowClosed(True)
    app.setStyleSheet(APP_STYLESHEET)

    window = MainWindow(daemon_thread=daemon_thread, log_buffer=log_buffer)
    window.show()

    return app.exec()
