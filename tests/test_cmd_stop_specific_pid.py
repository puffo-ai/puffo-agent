"""cmd_stop tracks the specific pid it asked to stop, not whatever's in
the pid file. On upgrade the pid file swaps to a new daemon mid-poll;
tracking the original pid lets cmd_stop report the swap instead of a
misleading "still running (pid=<old>)".
"""
from __future__ import annotations

import argparse
from unittest.mock import patch

from puffo_agent.portal import cli


def _args(timeout: int = 5) -> argparse.Namespace:
    return argparse.Namespace(timeout=timeout)


class TestCmdStopOriginalPid:
    def test_no_pid_file_reports_not_running(self, capsys):
        with patch("puffo_agent.portal.cli.read_daemon_pid", return_value=None):
            rc = cli.cmd_stop(_args())
        assert rc == 0
        assert "not running" in capsys.readouterr().out

    def test_stale_pid_file_clears_and_reports(self, capsys):
        with patch("puffo_agent.portal.cli.read_daemon_pid", return_value=1234), \
             patch("puffo_agent.portal.cli.is_pid_alive", return_value=False), \
             patch("puffo_agent.portal.cli.clear_daemon_pid") as clear:
            rc = cli.cmd_stop(_args())
        assert rc == 0
        clear.assert_called_once()
        assert "stale pid" in capsys.readouterr().out

    def test_daemon_stops_within_timeout(self, capsys):
        # Original daemon (pid 1234) alive at entry, dies on first poll.
        alive_calls = iter([True, False])
        # Pid file still shows the original pid throughout — no swap.
        with patch("puffo_agent.portal.cli.read_daemon_pid", side_effect=[1234, 1234]), \
             patch(
                "puffo_agent.portal.cli.is_pid_alive",
                side_effect=lambda pid: next(alive_calls),
            ), \
             patch("puffo_agent.portal.cli.write_stop_request"), \
             patch("puffo_agent.portal.cli.clear_stop_request"), \
             patch("puffo_agent.portal.cli.time.sleep"):
            rc = cli.cmd_stop(_args(timeout=5))
        assert rc == 0
        out = capsys.readouterr().out
        assert "daemon stopped" in out
        # No "new daemon" surface — same pid throughout.
        assert "new daemon" not in out

    def test_daemon_swap_mid_poll_surfaces_new_pid(self, capsys):
        """Original 1234 dies; pid file now shows 9999 (new daemon up).

        Before the fix, this scenario produced ``warning: daemon still
        running after Ns (pid=1234)`` because ``is_daemon_alive()`` saw
        the new pid as 'still alive.' After the fix, the original pid
        polls dead and we surface the swap.
        """
        original_pid = 1234
        new_pid = 9999

        # is_pid_alive returns True for original on entry, False once
        # the swap happens; True for new_pid when we check it post-swap.
        def pid_alive(pid):
            if pid == original_pid:
                # First call (initial check): alive. Second (poll): dead.
                return next(alive_iter)
            if pid == new_pid:
                return True
            return False

        alive_iter = iter([True, False])

        # read_daemon_pid: initial read at cmd_stop entry returns the
        # ORIGINAL pid (matches what the user typed-against). After the
        # original pid polls dead, the next read returns the new pid
        # (the new daemon has written it).
        with patch(
            "puffo_agent.portal.cli.read_daemon_pid",
            side_effect=[original_pid, new_pid],
        ), \
             patch("puffo_agent.portal.cli.is_pid_alive", side_effect=pid_alive), \
             patch("puffo_agent.portal.cli.write_stop_request"), \
             patch("puffo_agent.portal.cli.clear_stop_request"), \
             patch("puffo_agent.portal.cli.time.sleep"):
            rc = cli.cmd_stop(_args(timeout=5))
        assert rc == 0
        out = capsys.readouterr().out
        assert f"daemon stopped (pid={original_pid})" in out
        assert f"new daemon" in out and f"pid={new_pid}" in out

    def test_swap_message_skipped_when_new_pid_in_file_is_not_alive(self, capsys):
        """Pid file changed mid-poll but the new pid isn't a live
        daemon (stale write, race, etc.). The swap-message branch
        is gated on ``is_pid_alive(new_pid)`` AND ``new_pid != pid``;
        when the new pid is dead we should print plain
        ``"daemon stopped"`` — not surface a phantom swap.
        """
        original_pid = 1234
        bogus_new_pid = 9999  # written to pid file but not alive

        def pid_alive(pid):
            if pid == original_pid:
                return next(alive_iter)
            if pid == bogus_new_pid:
                return False  # the load-bearing gate
            return False

        alive_iter = iter([True, False])

        with patch(
            "puffo_agent.portal.cli.read_daemon_pid",
            side_effect=[original_pid, bogus_new_pid],
        ), \
             patch("puffo_agent.portal.cli.is_pid_alive", side_effect=pid_alive), \
             patch("puffo_agent.portal.cli.write_stop_request"), \
             patch("puffo_agent.portal.cli.clear_stop_request"), \
             patch("puffo_agent.portal.cli.time.sleep"):
            rc = cli.cmd_stop(_args(timeout=5))
        assert rc == 0
        out = capsys.readouterr().out
        assert "daemon stopped" in out
        assert "new daemon" not in out

    def test_daemon_does_not_stop_within_timeout(self, capsys):
        """Original pid keeps reporting alive past timeout → exit 1."""
        # Entry: alive. Poll forever: alive (we control time via sleep mock).
        with patch("puffo_agent.portal.cli.read_daemon_pid", return_value=1234), \
             patch("puffo_agent.portal.cli.is_pid_alive", return_value=True), \
             patch("puffo_agent.portal.cli.write_stop_request"), \
             patch("puffo_agent.portal.cli.time.sleep"), \
             patch(
                 "puffo_agent.portal.cli.time.time",
                 side_effect=[0, 0, 100, 100, 100],
             ):
            rc = cli.cmd_stop(_args(timeout=5))
        assert rc == 1
        # Warning cites the original pid (NOT some swapped pid).
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "pid=1234" in captured.err
