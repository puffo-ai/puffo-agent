"""PUF-375 (a): RuntimeConfig.task_timeout_seconds round-trips through
agent.yml, defaults to 600s, and reaches the CodexSession."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def test_default_is_600():
    from puffo_agent.portal.state import RuntimeConfig
    assert RuntimeConfig().task_timeout_seconds == 600.0


def test_round_trips(home):
    from puffo_agent.portal.state import AgentConfig, RuntimeConfig
    cfg = AgentConfig(
        id="codex-slow",
        display_name="codex-slow",
        runtime=RuntimeConfig(kind="cli-local", harness="codex", task_timeout_seconds=1800.0),
    )
    cfg.save()
    assert AgentConfig.load("codex-slow").runtime.task_timeout_seconds == 1800.0


def test_legacy_yml_without_field_defaults_600(home):
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    aid = "legacy-codex"
    yml = agent_yml_path(aid)
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        "id: legacy-codex\n"
        "state: running\n"
        "display_name: legacy-codex\n"
        "created_at: 0\n"
        "puffo_core: {server_url: 'https://api.puffo.ai', slug: '', "
        "device_id: '', space_id: '', operator_slug: ''}\n"
        "runtime: {kind: cli-local, provider: '', model: '', harness: codex, "
        "sandbox: danger-full-access}\n"
        "profile: profile.md\n"
        "memory_dir: memory\n"
        "workspace_dir: workspace\n"
        "triggers: {on_mention: true, on_dm: true}\n",
        encoding="utf-8",
    )
    assert AgentConfig.load(aid).runtime.task_timeout_seconds == 600.0


def test_codex_session_stores_timeout():
    from puffo_agent.agent.adapters.codex_session import CodexSession
    s = CodexSession(
        agent_id="a",
        session_file=Path("/tmp/nonexistent-codex-session.json"),
        argv=["codex", "app-server"],
        task_timeout_seconds=42.0,
    )
    assert s.task_timeout_seconds == 42.0
