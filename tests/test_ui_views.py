"""Qt view logic for the log tail + the grouped MCP status page, run on
the offscreen platform (no display needed)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# PySide6 now ships only in the ``[gui]`` extra (the base install is
# Qt-free for headless/cloud daemons). Skip the Qt view tests when the
# extra isn't installed rather than erroring out collection.
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from puffo_agent.portal.ui.widgets.log_view import LogView
from puffo_agent.portal.ui.widgets.mcp_status import McpStatusView, _status_style


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── log view: always tails the newest, even after the ring rolls ───


def test_log_view_initial_render(qapp):
    state = {"lines": ["l0", "l1", "l2"], "total": 3}
    v = LogView(lambda: state["lines"], lambda: state["total"])
    v.poll()
    assert v.toPlainText().splitlines() == ["l0", "l1", "l2"]


def test_log_view_tails_after_buffer_rolls(qapp):
    """Regression: the ring buffer drops its oldest line, so length
    stays pinned — the old length-diff froze on the first lines. The
    counter-diff must keep showing the newest."""
    state = {"lines": ["l0", "l1", "l2"], "total": 3}
    v = LogView(lambda: state["lines"], lambda: state["total"])
    v.poll()
    state["lines"], state["total"] = ["l1", "l2", "l3"], 4
    v.poll()
    assert v.toPlainText().splitlines()[-1] == "l3"
    state["lines"], state["total"] = ["l2", "l3", "l4"], 5
    v.poll()
    assert v.toPlainText().splitlines()[-1] == "l4"


def test_log_view_filter(qapp):
    state = {"lines": ["keep1", "drop", "keep2"], "total": 3}
    v = LogView(
        lambda: state["lines"], lambda: state["total"],
        filter_fn=lambda line: "keep" in line,
    )
    v.poll()
    assert v.toPlainText().splitlines() == ["keep1", "keep2"]


# ── status page: grouped by agent + status colour ──────────────────


def test_status_style_maps_and_colours():
    label, color = _status_style("sleeping")
    assert label == "running"                       # idle relabelled
    assert color.name() == "#16a34a"                # green
    assert _status_style("zombie")[1].name() == "#dc2626"   # red


def test_mcp_status_groups_by_agent(qapp):
    w = McpStatusView()
    w._probe.sample = lambda: [  # type: ignore[assignment]
        {"agent": "a1", "agent_name": "Alice", "server": "playwright",
         "pid": 10, "status": "sleeping", "cpu": 1.0, "mem_mb": 50.0},
        {"agent": "a1", "agent_name": "Alice", "server": "filesystem",
         "pid": 11, "status": "running", "cpu": 0.0, "mem_mb": 30.0},
        {"agent": "a2", "agent_name": "Bob", "server": "git",
         "pid": 12, "status": "zombie", "cpu": 0.0, "mem_mb": 5.0},
    ]
    w.poll()
    assert "3 running" in w._title.text()
    tree = w._tree
    assert tree.topLevelItemCount() == 2          # Alice, Bob
    alice = tree.topLevelItem(0)                   # sorted by name
    assert "Alice" in alice.text(0)
    assert alice.childCount() == 2                 # playwright + filesystem
