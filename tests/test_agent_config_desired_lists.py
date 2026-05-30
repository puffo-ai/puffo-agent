"""PUF-268: AgentConfig.desired_skills + AgentConfig.desired_mcps
round-trip through agent.yml save/load. These are template-id lists
the daemon installs at spawn time AFTER host-sync — operator-curated
adds on top of whatever the host already provides.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _make_cfg(agent_id: str, **kwargs):
    from puffo_agent.portal.state import AgentConfig
    return AgentConfig(id=agent_id, display_name=agent_id, **kwargs)


def test_default_desired_lists_are_empty(home):
    cfg = _make_cfg("agent-defaults")
    assert cfg.desired_skills == []
    assert cfg.desired_mcps == []


def test_save_then_load_round_trips_both_lists(home):
    from puffo_agent.portal.state import AgentConfig
    cfg = _make_cfg(
        "agent-skills",
        desired_skills=["git-pr-flow", "pytest-tdd"],
        desired_mcps=["filesystem", "github", "fetch"],
    )
    cfg.save()
    loaded = AgentConfig.load("agent-skills")
    assert loaded.desired_skills == ["git-pr-flow", "pytest-tdd"]
    assert loaded.desired_mcps == ["filesystem", "github", "fetch"]


def test_load_legacy_yml_without_desired_fields_defaults_to_empty(home, tmp_path):
    # Back-compat: agents created before PUF-268 don't carry these
    # fields in agent.yml. Load must default to [] not KeyError.
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    aid = "legacy-agent"
    yml = agent_yml_path(aid)
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        "id: legacy-agent\n"
        "state: running\n"
        "display_name: legacy-agent\n"
        "role: ''\n"
        "role_short: ''\n"
        "created_at: 0\n"
        "puffo_core: {server_url: 'https://api.puffo.ai', slug: '', device_id: '', space_id: '', operator_slug: ''}\n"
        "runtime: {kind: chat-local, provider: '', model: '', api_key: '', "
        "harness: claude-code, permission_mode: bypassPermissions, max_turns: 10, "
        "allowed_tools: [], docker_image: '', docker_memory_limit: '', "
        "docker_memory_reservation: ''}\n"
        "profile: profile.md\n"
        "memory_dir: memory\n"
        "workspace_dir: workspace\n"
        "triggers: {on_mention: true, on_dm: true}\n",
        encoding="utf-8",
    )
    cfg = AgentConfig.load(aid)
    assert cfg.desired_skills == []
    assert cfg.desired_mcps == []


def test_save_persists_empty_lists_explicitly(home, tmp_path):
    # Defaults are []; on save, the YAML must include the keys
    # explicitly so a downstream reader can tell "no desired list"
    # from "field missing entirely."
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    cfg = _make_cfg("agent-explicit-empty")
    cfg.save()
    body = agent_yml_path("agent-explicit-empty").read_text(encoding="utf-8")
    assert "desired_skills:" in body
    assert "desired_mcps:" in body
