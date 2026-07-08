"""Slim-packaging guard (Item A): the daemon / CLI path must import and
run with PySide6 absent, and a GUI command without the ``[gui]`` extra
must fail with the actionable install hint — not a raw traceback.

Deterministic in any environment: we block ``PySide6`` (and its
submodules) via ``sys.modules[...] = None`` regardless of whether the
extra happens to be installed, so this asserts the real headless
contract rather than "the test box didn't have Qt".
"""

from __future__ import annotations

import argparse
import importlib
import sys

import pytest


# Every PySide6 entry point the UI modules reach for. Mapping a name to
# ``None`` in sys.modules makes ``import <name>`` raise ImportError even
# when the package is installed — the standard "pretend it's missing"
# trick.
_PYSIDE_NAMES = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]


@pytest.fixture
def pyside6_blocked(monkeypatch):
    """Block PySide6 and drop cached puffo_agent modules that could hold
    a live reference, so imports re-run under the block."""
    for name in _PYSIDE_NAMES:
        monkeypatch.setitem(sys.modules, name, None)
    # Drop cached daemon/cli/ui modules so the assertions below exercise
    # a fresh import under the block rather than a warm module object.
    for name in list(sys.modules):
        if name == "puffo_agent.portal.daemon" or name == "puffo_agent.portal.cli":
            monkeypatch.delitem(sys.modules, name, raising=False)
        elif name.startswith("puffo_agent.portal.ui"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    yield


def test_pyside6_is_actually_blocked(pyside6_blocked):
    """Sanity: the fixture makes ``import PySide6`` raise, so the two
    assertions below mean what they say."""
    with pytest.raises(ImportError):
        import PySide6  # noqa: F401


def test_daemon_and_cli_import_without_pyside6(pyside6_blocked):
    """The headless daemon path (`puffo-agent start` -> run_daemon) and
    the CLI module must import cleanly with Qt absent."""
    daemon = importlib.import_module("puffo_agent.portal.daemon")
    cli = importlib.import_module("puffo_agent.portal.cli")
    # run_daemon is the headless entry point cmd_start dispatches to.
    assert hasattr(daemon, "run_daemon")
    assert hasattr(cli, "cmd_start")


def test_gui_command_without_extra_yields_actionable_hint(
    pyside6_blocked, capsys,
):
    """`start --ui` with PySide6 absent returns non-zero and prints the
    `pip install 'puffo-agent[gui]'` hint instead of ModuleNotFoundError."""
    cli = importlib.import_module("puffo_agent.portal.cli")
    args = argparse.Namespace(
        ui=True, tray_runner=False, background=False, with_local_bridge=False,
    )
    rc = cli.cmd_start(args)
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "puffo-agent[gui]" in combined


def test_tray_command_without_extra_yields_actionable_hint(
    pyside6_blocked, capsys,
):
    """Same guard for the `start --tray-runner` entry point."""
    cli = importlib.import_module("puffo_agent.portal.cli")
    args = argparse.Namespace(
        ui=False, tray_runner=True, background=False, with_local_bridge=False,
    )
    rc = cli.cmd_start(args)
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "puffo-agent[gui]" in combined
