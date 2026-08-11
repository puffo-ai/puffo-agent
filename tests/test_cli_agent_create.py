from __future__ import annotations

from puffo_agent.portal.cli import main
from puffo_agent.portal.state import AgentConfig, agent_dir


def test_create_cli_docker_openai_is_rejected_before_writing(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "docker-codex",
        "--runtime", "cli-docker", "--provider", "openai",
    ])

    assert rc == 2
    assert "requires the host-local Driver runtime" in capsys.readouterr().err
    assert not agent_dir("docker-codex").exists()


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
