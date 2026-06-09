"""MCP probe helpers + log-buffer counter (the non-Qt parts of the UI
status page and the latest-X log fix)."""

from __future__ import annotations

import logging

from puffo_agent.portal.ui.log_buffer import LogRingHandler
from puffo_agent.portal.ui.mcp_probe import (
    McpProbe,
    agent_id_from_cwd,
    server_name,
)


def test_agent_id_from_cwd():
    root = r"C:\Users\h\.puffo-agent\agents"
    assert agent_id_from_cwd(
        r"C:\Users\h\.puffo-agent\agents\planner-9eaf\workspace", root
    ) == "planner-9eaf"
    # forward slashes + exact agent dir
    assert agent_id_from_cwd(
        "/home/h/.puffo-agent/agents/eng-1a/.codex", "/home/h/.puffo-agent/agents"
    ) == "eng-1a"
    # not under the agents root → None
    assert agent_id_from_cwd(r"C:\somewhere\else", root) is None


def test_server_name():
    assert server_name("npx @playwright/mcp@latest --browser=chromium") == "playwright"
    assert server_name("node @modelcontextprotocol/server-filesystem /tmp") == "filesystem"
    assert server_name("node mcp-server-git --repo .") == "git"
    # fallback: last token's basename
    assert server_name("node /opt/tools/my-thing.js") == "my-thing.js"


def test_mcp_probe_sample_is_safe():
    """Smoke: in a test process with no MCP children, sample() returns
    an empty list rather than raising."""
    assert McpProbe().sample() == []


def test_mcp_probe_sample_attributes_to_agent(monkeypatch):
    """A node MCP server is attributed to the agent that owns the
    claude session two levels up (node ← cmd/npx ← claude session)."""
    from pathlib import Path

    from puffo_agent.portal.ui import mcp_probe

    class _Mem:
        rss = 2 * 1024 * 1024

    tree: dict[int, "_FakeProc"] = {}

    class _FakeProc:
        def __init__(self, pid, ppid, name, cmd, cwd=""):
            self.pid = pid
            self._ppid, self._name, self._cmd, self._cwd = ppid, name, cmd, cwd

        def ppid(self):
            return self._ppid

        def name(self):
            return self._name

        def cmdline(self):
            return self._cmd

        def cwd(self):
            return self._cwd

        def status(self):
            return "running"

        def cpu_percent(self, _interval=None):
            return 0.0

        def memory_info(self):
            return _Mem()

        def children(self, recursive=False):
            return [p for p in tree.values() if p.pid != 1]

    for p in (
        _FakeProc(1, 0, "python", ["python"]),
        _FakeProc(2, 1, "claude.exe", ["claude", "--resume"],
                  cwd=r"C:\h\.puffo-agent\agents\a1\workspace"),
        _FakeProc(3, 2, "cmd.exe", ["cmd", "/c", "npx", "@playwright/mcp"]),
        _FakeProc(4, 3, "node.exe", ["node", "@playwright/mcp", "cli.js"]),
    ):
        tree[p.pid] = p

    class _FakePsutil:
        Process = staticmethod(lambda pid=1: tree[pid])

    monkeypatch.setattr(mcp_probe, "psutil", _FakePsutil)
    monkeypatch.setattr(mcp_probe.os, "getpid", lambda: 1)
    monkeypatch.setattr(
        mcp_probe, "agents_dir", lambda: Path(r"C:\h\.puffo-agent\agents"),
    )

    rows = mcp_probe.McpProbe().sample()
    assert len(rows) == 1
    assert rows[0]["agent"] == "a1"
    assert rows[0]["server"] == "playwright"
    assert rows[0]["pid"] == 4


def test_log_buffer_counter_monotonic_through_roll():
    h = LogRingHandler(maxlen=3)
    for i in range(5):
        h.emit(logging.LogRecord("x", logging.INFO, "", 0, f"line{i}", None, None))
    # buffer keeps the NEWEST 3, counter counts ALL 5 ever emitted
    snap = h.snapshot()
    assert len(snap) == 3
    assert snap[-1].endswith("line4")
    assert snap[0].endswith("line2")
    assert h.counter() == 5
