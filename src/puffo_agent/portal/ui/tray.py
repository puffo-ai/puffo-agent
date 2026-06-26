"""``puffo-agent start --tray-runner``: the daemon plus a status-bar
(system-tray) icon whose only action is Quit.

Spawned detached by ``start --background`` so it outlives the launching
terminal. Reuses the ``--ui`` integration (``DaemonThread`` runs the
asyncio daemon while Qt owns the main thread) but shows a tray icon
instead of the desktop window.
"""

from __future__ import annotations

import logging
import sys

from .daemon_thread import DaemonThread
from .log_buffer import install_log_buffer

logger = logging.getLogger(__name__)


def run_tray(with_local_bridge: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log_buffer = install_log_buffer(maxlen=500)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    from .assets import logo_path

    daemon_thread = DaemonThread(with_local_bridge=with_local_bridge)
    daemon_thread.start()

    app = QApplication(sys.argv)
    app.setApplicationName("Puffo Agent")
    # The tray icon is the only UI — without this the app would exit the
    # moment it's created (no windows means "last window closed").
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # GUI session but no tray host (some Linux DEs). Keep serving the
        # daemon headless; stop it with `puffo-agent stop`.
        logger.warning(
            "no system tray available; running headless in the background — "
            "stop with `puffo-agent stop`",
        )
        return app.exec()

    icon = QIcon(str(logo_path()))
    app.setWindowIcon(icon)
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Puffo Agent — running")

    # Lazily-opened desktop window. Detached so closing it just hides the
    # window — only Quit (below) stops the daemon.
    window: dict = {"w": None}

    def _open_ui() -> None:
        existing = window["w"]
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        from .main_window import MainWindow
        from .style import APP_STYLESHEET
        app.setStyleSheet(APP_STYLESHEET)
        win = MainWindow(
            daemon_thread=daemon_thread, log_buffer=log_buffer, detached=True,
        )
        window["w"] = win
        win.show()
        win.raise_()
        win.activateWindow()

    menu = QMenu()
    ui_action = menu.addAction("Open UI (beta)")
    ui_action.triggered.connect(_open_ui)
    quit_action = menu.addAction("Quit")

    def _quit() -> None:
        tray.hide()
        # Graceful: same sentinel path as `puffo-agent stop`. The daemon
        # tears down workers then ``os._exit(0)``s, ending the process;
        # ``app.quit()`` is the fallback when we don't own the PID file.
        daemon_thread.request_stop()
        app.quit()

    quit_action.triggered.connect(_quit)
    tray.setContextMenu(menu)
    tray.show()

    return app.exec()
