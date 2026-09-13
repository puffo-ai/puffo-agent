"""Guard environment targeting, lazy setup, and GUI failure semantics."""

import argparse
import subprocess
import sys
from types import SimpleNamespace

import pytest

from puffo_agent.portal import desktop_dependencies as desktop


@pytest.mark.parametrize("installer", ["uv", "pip"])
def test_explicit_ui_prepares_only_qt_in_current_interpreter(monkeypatch, installer):
    """GUI setup must not install into PATH Python or change puffo-agent's version."""
    from puffo_agent.portal import cli

    installed = False
    calls = []
    monkeypatch.setattr(
        desktop.shutil, "which", lambda _: "/tools/uv" if installer == "uv" else None
    )
    monkeypatch.setattr(
        desktop.importlib.util,
        "find_spec",
        lambda name: object() if installed or name == "pip" else None,
    )

    def run(command, **kwargs):
        nonlocal installed
        calls.append(command)
        installed = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    original_import = desktop.importlib.import_module
    monkeypatch.setattr(
        desktop.importlib,
        "import_module",
        lambda name, *args: (
            object() if name == "PySide6.QtWidgets" else original_import(name, *args)
        ),
    )
    monkeypatch.setitem(
        sys.modules, "puffo_agent.portal.ui.launcher", SimpleNamespace(launch=lambda: 4)
    )
    args = argparse.Namespace(ui=True, tray_runner=False, background=False)
    assert cli.cmd_start(args) == 4
    prefix = (
        ["/tools/uv", "pip", "install", "--python", sys.executable]
        if installer == "uv"
        else [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    )
    assert calls == [prefix + ["pyside6>=6.7"]]
    # A second launch must not contact an installer when Qt is already present.
    assert cli.cmd_start(args) == 4
    assert len(calls) == 1


def test_broken_shared_library_does_not_trigger_reinstallation(monkeypatch):
    """libGL failures must not repeatedly download an already installed Qt."""
    monkeypatch.setattr(desktop.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(
        desktop.subprocess, "run", lambda *a, **k: pytest.fail("must not reinstall")
    )

    def broken_import(_):
        raise ImportError("libGL.so.1: cannot open shared object file")

    monkeypatch.setattr(desktop.importlib, "import_module", broken_import)
    with pytest.raises(ImportError, match="libGL.so.1"):
        desktop.prepare_desktop()


def test_existing_qt_below_minimum_is_upgraded(monkeypatch):
    """Removing Qt from Linux base requirements must not leave unsupported old Qt."""
    calls = []
    monkeypatch.setattr(desktop.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(desktop.importlib.metadata, "version", lambda _: "6.6.3")
    monkeypatch.setattr(desktop.shutil, "which", lambda _: "/tools/uv")
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(desktop.importlib, "import_module", lambda _: object())
    desktop.prepare_desktop()
    assert calls == [
        ["/tools/uv", "pip", "install", "--python", sys.executable, "pyside6>=6.7"]
    ]


@pytest.mark.parametrize("has_pip", [True, False])
def test_user_install_preserves_user_site_even_when_uv_is_on_path(
    monkeypatch, tmp_path, has_pip
):
    """pip --user users must not be redirected into a system installation."""
    calls = []
    user_site = tmp_path / "site-packages"
    monkeypatch.setattr(desktop.site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(desktop.site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(
        desktop.importlib.metadata,
        "distribution",
        lambda _: SimpleNamespace(locate_file=lambda _: user_site),
    )
    monkeypatch.setattr(
        desktop.importlib.util,
        "find_spec",
        lambda name: object() if name == "pip" and has_pip else None,
    )
    monkeypatch.setattr(desktop.shutil, "which", lambda _: "/tools/uv")
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(desktop.importlib, "import_module", lambda _: object())
    if not has_pip:
        with pytest.raises(ImportError, match="user installation requires pip"):
            desktop.prepare_desktop()
        assert calls == []
        return
    desktop.prepare_desktop()
    assert calls == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--user",
            "pyside6>=6.7",
        ]
    ]


@pytest.mark.parametrize("failure", [1, "timeout", "missing_installer"])
def test_gui_setup_failure_remains_a_gui_failure(monkeypatch, capsys, failure):
    """Failed downloads must not launch UI or silently start a headless daemon."""
    from puffo_agent.portal import cli

    monkeypatch.setattr(desktop.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(
        desktop.shutil,
        "which",
        lambda _: None if failure == "missing_installer" else "/tools/uv",
    )

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 600)
        return SimpleNamespace(returncode=failure)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    monkeypatch.setitem(
        sys.modules,
        "puffo_agent.portal.ui.launcher",
        SimpleNamespace(launch=lambda: pytest.fail("setup failed")),
    )
    assert cli.cmd_start(argparse.Namespace(ui=True)) == 1
    assert "Desktop support could not start" in capsys.readouterr().err


def test_headless_start_never_prepares_gui(monkeypatch):
    """A server's ordinary start remains offline with respect to desktop setup."""
    from puffo_agent.portal import cli

    async def daemon():
        return 7

    monkeypatch.setattr(
        desktop.subprocess, "run", lambda *a, **k: pytest.fail("headless installation")
    )
    monkeypatch.setitem(
        sys.modules, "puffo_agent.portal.daemon", SimpleNamespace(run_daemon=daemon)
    )
    assert cli.cmd_start(argparse.Namespace()) == 7
