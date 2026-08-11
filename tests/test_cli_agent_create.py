from __future__ import annotations

from puffo_agent.portal.cli import main
from puffo_agent.portal.state import AgentConfig, agent_dir


def test_create_cli_docker_openai_defaults_to_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "docker-codex",
        "--runtime", "cli-docker", "--provider", "openai",
    ])

    assert rc == 0
    cfg = AgentConfig.load("docker-codex")
    assert cfg.runtime.provider == "openai"
    assert cfg.runtime.harness == "codex"


def test_create_rejects_invalid_cli_docker_harness_before_writing(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "invalid-docker",
        "--runtime", "cli-docker", "--provider", "openai",
        "--harness", "claude-code",
    ])

    assert rc == 2
    assert "does not support provider 'openai'" in capsys.readouterr().err
    assert not agent_dir("invalid-docker").exists()
