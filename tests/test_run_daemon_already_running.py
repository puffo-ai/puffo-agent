"""run_daemon's already-running path returns exit 0 (info log), not
exit 1 (error). ``start`` against a live daemon meets the user's intent;
single-daemon enforcement is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from puffo_agent.portal.daemon import run_daemon


class TestRunDaemonAlreadyRunning:
    def test_returns_0_when_daemon_already_alive(self, caplog):
        with patch("puffo_agent.portal.daemon.is_daemon_alive", return_value=True), \
             patch("puffo_agent.portal.daemon.read_daemon_pid", return_value=4242), \
             caplog.at_level(logging.INFO):
            rc = asyncio.run(run_daemon())
        assert rc == 0

    def test_logs_at_info_not_error_when_already_running(self, caplog):
        # An already-running daemon is the user's intent, not an error —
        # log at INFO, not ERROR.
        with patch("puffo_agent.portal.daemon.is_daemon_alive", return_value=True), \
             patch("puffo_agent.portal.daemon.read_daemon_pid", return_value=4242), \
             caplog.at_level(logging.INFO):
            asyncio.run(run_daemon())
        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        err_msgs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("already running" in r.message for r in info_msgs)
        assert err_msgs == []

    def test_message_carries_existing_daemon_pid(self, caplog):
        # The user needs the pid to look up + manage the running daemon.
        with patch("puffo_agent.portal.daemon.is_daemon_alive", return_value=True), \
             patch("puffo_agent.portal.daemon.read_daemon_pid", return_value=4242), \
             caplog.at_level(logging.INFO):
            asyncio.run(run_daemon())
        assert any("4242" in r.message for r in caplog.records)

    def test_message_also_prints_to_stdout_for_background_runners(self, capsys):
        """tray + background runners may not surface INFO logs, so the
        "already running" message also goes to stdout."""
        with patch("puffo_agent.portal.daemon.is_daemon_alive", return_value=True), \
             patch("puffo_agent.portal.daemon.read_daemon_pid", return_value=4242):
            asyncio.run(run_daemon())
        out = capsys.readouterr().out
        assert "already running" in out
        assert "4242" in out
