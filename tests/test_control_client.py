"""execute_command applies portal commands to local agent state."""

from __future__ import annotations

import pytest

from _bridge_support import isolated_home, write_test_agent
from puffo_agent.portal.control.client import execute_command
from puffo_agent.portal.state import AgentConfig


@pytest.fixture
def home(monkeypatch):
    h = isolated_home()
    yield h


def test_pause_then_resume(home):
    write_test_agent(home, "scout")
    assert AgentConfig.load("scout").state == "running"

    res = execute_command("pause", "scout", {})
    assert res["ok"] is True
    assert AgentConfig.load("scout").state == "paused"

    res = execute_command("resume", "scout", {})
    assert res["ok"] is True
    assert AgentConfig.load("scout").state == "running"


def test_edit_display_name_and_role(home):
    write_test_agent(home, "scout")
    res = execute_command("edit", "scout", {"display_name": "Scout One", "role": "researcher"})
    assert res["ok"] is True
    cfg = AgentConfig.load("scout")
    assert cfg.display_name == "Scout One"
    assert cfg.role == "researcher"


def test_archive_drops_flag(home):
    from puffo_agent.portal.state import archive_flag_path

    write_test_agent(home, "scout")
    res = execute_command("archive", "scout", {})
    assert res["ok"] is True
    assert archive_flag_path("scout").exists()


def test_unknown_agent_rejected(home):
    res = execute_command("pause", "ghost", {})
    assert res["ok"] is False


def test_unsupported_op_rejected(home):
    write_test_agent(home, "scout")
    res = execute_command("create", "scout", {})
    assert res["ok"] is False
