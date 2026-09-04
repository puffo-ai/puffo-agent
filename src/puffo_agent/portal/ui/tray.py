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

from .daemon_thread import DaemonThread, install_daemon_watchdog
from .log_buffer import install_log_buffer

logger = logging.getLogger(__name__)

# Token from NSProcessInfo.beginActivityWithOptions: — held for the process
# lifetime; releasing it would re-enable App Nap.
_app_nap_activity_token = None


def _exempt_from_app_nap() -> None:
    """Best-effort macOS App Nap exemption for the tray process.

    The daemon runs as a thread of this windowless Qt application, and
    macOS suspends napping GUI apps wholesale — in the 8/30 incident the
    daemon froze for whole hours on an awake machine, resuming only when
    the user touched the computer. NSActivityUserInitiatedAllowingIdle-
    SystemSleep keeps the process running without preventing normal
    system sleep. No-op off macOS; any failure is logged and ignored —
    a missing exemption degrades to today's behavior, never worse.
    """
    global _app_nap_activity_token
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p

        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        info = send(
            objc.objc_getClass(b"NSProcessInfo"),
            objc.sel_registerName(b"processInfo"),
        )
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
        reason = send(
            objc.objc_getClass(b"NSString"),
            objc.sel_registerName(b"stringWithUTF8String:"),
            b"Puffo agents serve messages while unattended",
        )
        # NSActivityUserInitiated minus NSActivityIdleSystemSleepDisabled
        # (1 << 20): defeat App Nap, still allow the system to sleep.
        options = ctypes.c_uint64(0x00FFFFFF & ~(1 << 20))
        send.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
        ]
        token = send(
            info,
            objc.sel_registerName(b"beginActivityWithOptions:reason:"),
            options,
            reason,
        )
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        send(token, objc.sel_registerName(b"retain"))
        _app_nap_activity_token = token
        logger.info("App Nap exemption active for the tray-hosted daemon")
    except Exception:
        logger.warning(
            "could not exempt the tray from App Nap; the daemon may be "
            "suspended while the machine is unattended",
            exc_info=True,
        )


def run_tray() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log_buffer = install_log_buffer(maxlen=500)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    from .assets import logo_path

    app = QApplication(sys.argv)
    app.setApplicationName("Puffo Agent")
    _exempt_from_app_nap()
    # The tray icon is the only UI — without this the app would exit the
    # moment it's created (no windows means "last window closed").
    app.setQuitOnLastWindowClosed(False)

    daemon_thread = DaemonThread()
    daemon_thread.start()
    daemon_watchdog = install_daemon_watchdog(app, daemon_thread)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # GUI session but no tray host (some Linux DEs). Keep serving the
        # daemon headless; stop it with `puffo-agent stop`.
        logger.warning(
            "no system tray available; running headless in the background — "
            "stop with `puffo-agent stop`",
        )
        qt_exit_code = app.exec()
        if daemon_thread.failed:
            return daemon_thread.exit_code or 1
        return qt_exit_code

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

    qt_exit_code = app.exec()
    # Keep the timer strongly referenced through the event loop.
    _ = daemon_watchdog
    if daemon_thread.failed:
        return daemon_thread.exit_code or 1
    return qt_exit_code
