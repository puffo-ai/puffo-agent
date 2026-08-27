from __future__ import annotations

from puffo_agent.portal.cli import main
from puffo_agent.portal.state import AgentConfig


def test_create_cli_docker_openai_persists_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "docker-codex",
        "--runtime", "cli-docker", "--provider", "openai",
    ])

    assert rc == 0
    cfg = AgentConfig.load("docker-codex")
    assert cfg.runtime.provider == "openai"
    assert cfg.runtime.harness == "codex"


def test_create_cli_docker_anthropic_uses_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "docker-claude",
        "--runtime", "cli-docker", "--provider", "anthropic",
    ])

    assert rc == 0
    cfg = AgentConfig.load("docker-claude")
    assert cfg.runtime.provider == "anthropic"
    assert cfg.runtime.harness == "claude-code"
