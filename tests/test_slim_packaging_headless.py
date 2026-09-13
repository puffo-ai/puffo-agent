"""Slim-packaging guard (Item A): the daemon / CLI path must import and
run with PySide6 absent, and failed automatic GUI setup must return an
actionable error rather than a raw traceback or a main-package reinstall.

Deterministic in any environment: we block ``PySide6`` (and its
submodules) via ``sys.modules[...] = None`` regardless of whether the
package happens to be installed, so this asserts the real headless
contract rather than "the test box didn't have Qt".
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "system, required", [("Darwin", True), ("Windows", True), ("Linux", False)]
)
def test_plain_install_selects_gui_dependency_by_system(system, required):
    """Linux servers must install without Qt; desktop defaults survive upgrades."""
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]
    from packaging.requirements import Requirement

    requirements = [Requirement(value) for value in project["dependencies"]]
    assert (
        any(
            req.name.lower() == "pyside6"
            and (req.marker is None or req.marker.evaluate({"platform_system": system}))
            for req in requirements
        )
        is required
    )


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
    from puffo_agent.portal import desktop_dependencies
    from types import SimpleNamespace

    monkeypatch.setattr(
        desktop_dependencies.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )
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


def test_cli_import_does_not_eagerly_import_daemon(pyside6_blocked):
    importlib.import_module("puffo_agent.portal.cli")

    assert "puffo_agent.portal.daemon" not in sys.modules


def test_gui_command_with_missing_dependency_yields_actionable_hint(
    pyside6_blocked,
    capsys,
):
    """Failed `start --ui` setup must not recommend a potentially downgrading reinstall."""
    cli = importlib.import_module("puffo_agent.portal.cli")
    args = argparse.Namespace(
        ui=True,
        tray_runner=False,
        background=False,
        with_local_bridge=False,
    )
    rc = cli.cmd_start(args)
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Desktop support could not start" in combined
    assert "force-reinstall" not in combined


def test_tray_command_with_missing_dependency_yields_actionable_hint(
    pyside6_blocked,
    capsys,
):
    """Same guard for the `start --tray-runner` entry point."""
    cli = importlib.import_module("puffo_agent.portal.cli")
    args = argparse.Namespace(
        ui=False,
        tray_runner=True,
        background=False,
        with_local_bridge=False,
    )
    rc = cli.cmd_start(args)
    assert rc != 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Desktop support could not start" in combined
    assert "force-reinstall" not in combined
