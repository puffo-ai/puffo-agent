"""uv-tool install detection + upgrade-command branching.

Users on uv-managed Python hit PEP 668 ``externally-managed-environment``
on ``pip install``; runtime detection picks the matching upgrade command.
"""
from __future__ import annotations

from unittest.mock import patch

from puffo_agent.portal.cli import (
    _is_uv_tool_install,
    upgrade_command_for_install_mode,
)


class TestIsUvToolInstall:
    def test_detects_posix_uv_tool_prefix(self):
        with patch("sys.prefix", "/Users/sam/.local/share/uv/tools/puffo-agent"):
            assert _is_uv_tool_install() is True

    def test_detects_windows_uv_tool_prefix(self):
        with patch("sys.prefix", "C:\\Users\\sam\\AppData\\Roaming\\uv\\tools\\puffo-agent"):
            assert _is_uv_tool_install() is True

    def test_rejects_homebrew_python_prefix(self):
        with patch("sys.prefix", "/opt/homebrew/Cellar/python@3.13/3.13.0/Frameworks/Python.framework/Versions/3.13"):
            assert _is_uv_tool_install() is False

    def test_rejects_pip_user_install_prefix(self):
        with patch("sys.prefix", "/Users/sam/.local"):
            assert _is_uv_tool_install() is False

    def test_rejects_virtualenv_prefix(self):
        with patch("sys.prefix", "/Users/sam/projects/myproj/.venv"):
            assert _is_uv_tool_install() is False


class TestUpgradeCommandForInstallMode:
    def test_source_install_path_wins(self):
        with patch("puffo_agent.portal.cli.is_source_install", return_value=True):
            cmd = upgrade_command_for_install_mode()
        assert "git+https" in cmd
        assert "pip install --upgrade --user" in cmd

    def test_uv_tool_install_returns_uv_command(self):
        with patch("puffo_agent.portal.cli.is_source_install", return_value=False), \
             patch("puffo_agent.portal.cli._is_uv_tool_install", return_value=True):
            cmd = upgrade_command_for_install_mode()
        assert cmd == "uv tool install puffo-agent --force"

    def test_default_returns_pip_upgrade(self):
        with patch("puffo_agent.portal.cli.is_source_install", return_value=False), \
             patch("puffo_agent.portal.cli._is_uv_tool_install", return_value=False):
            cmd = upgrade_command_for_install_mode()
        assert cmd == "pip install --upgrade puffo-agent"

    def test_source_install_precedence_over_uv_detection(self):
        # Source install wins even if sys.prefix also matches uv-tool —
        # the git-checkout dev path always upgrades from VCS, not PyPI.
        with patch("puffo_agent.portal.cli.is_source_install", return_value=True), \
             patch("puffo_agent.portal.cli._is_uv_tool_install", return_value=True):
            cmd = upgrade_command_for_install_mode()
        assert "git+https" in cmd
        assert "uv tool" not in cmd
