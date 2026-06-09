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
