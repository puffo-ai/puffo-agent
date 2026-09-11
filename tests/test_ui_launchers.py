from __future__ import annotations

from pathlib import Path

from PySide6 import QtGui, QtWidgets

from puffo_agent.portal.ui import launcher, tray
from puffo_agent.portal.ui import assets, main_window


class FakeDaemonThread:
    started = 0
    failed = False
    exit_code = 0

    def start(self):
        type(self).started += 1

    def is_alive(self):
        return True


class FakeApplication:
    def __init__(self, _argv):
        self.quit_on_close = None

    def setApplicationName(self, _name):
        return None

    def setWindowIcon(self, _icon):
        return None

    def setQuitOnLastWindowClosed(self, value):
        self.quit_on_close = value

    def setStyleSheet(self, _stylesheet):
        return None

    def exec(self):
        return 7


class FakeWindow:
    def __init__(self, **_kwargs):
        self.shown = False

    def show(self):
        self.shown = True


def _patch_common(monkeypatch, module):
    FakeDaemonThread.started = 0
    FakeDaemonThread.failed = False
    FakeDaemonThread.exit_code = 0
    monkeypatch.setattr(module, "DaemonThread", FakeDaemonThread)
    monkeypatch.setattr(module, "install_log_buffer", lambda **_kwargs: object())
    monkeypatch.setattr(QtWidgets, "QApplication", FakeApplication)
    monkeypatch.setattr(QtGui, "QIcon", lambda _path: object())
    monkeypatch.setattr(assets, "logo_path", lambda: Path("logo.png"))
    monkeypatch.setattr(
        module,
        "install_daemon_watchdog",
        lambda _app, _thread: object(),
    )


def test_launcher_starts_default_daemon_thread(monkeypatch):
    _patch_common(monkeypatch, launcher)
    monkeypatch.setattr(main_window, "MainWindow", FakeWindow)
    assert launcher.launch() == 7
    assert FakeDaemonThread.started == 1


def test_launcher_returns_daemon_startup_failure(monkeypatch):
    _patch_common(monkeypatch, launcher)
    FakeDaemonThread.failed = True
    FakeDaemonThread.exit_code = 1
    monkeypatch.setattr(main_window, "MainWindow", FakeWindow)

    assert launcher.launch() == 1


def test_tray_starts_default_daemon_thread(monkeypatch):
    _patch_common(monkeypatch, tray)
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon,
        "isSystemTrayAvailable",
        staticmethod(lambda: False),
    )
    assert tray.run_tray() == 7
    assert FakeDaemonThread.started == 1


def test_tray_returns_daemon_startup_failure(monkeypatch):
    _patch_common(monkeypatch, tray)
    FakeDaemonThread.failed = True
    FakeDaemonThread.exit_code = 1
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon,
        "isSystemTrayAvailable",
        staticmethod(lambda: False),
    )

    assert tray.run_tray() == 1
