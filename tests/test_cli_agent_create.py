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


def test_create_generic_acp_persists_literal_command_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "acp-agent",
        "--provider", "google", "--harness", "acp",
        "--harness-command", "opencode", "acp",
    ])

    assert rc == 0
    cfg = AgentConfig.load("acp-agent")
    assert cfg.runtime.harness == "acp"
    assert cfg.runtime.harness_command == ["opencode", "acp"]


def test_create_generic_acp_requires_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main(["agent", "create", "--id", "bad-acp", "--harness", "acp"])

    assert rc == 2
    assert "requires --harness-command" in capsys.readouterr().err


def test_create_pi_uses_builtin_binary_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    rc = main([
        "agent", "create", "--id", "pi-agent",
        "--provider", "openai", "--harness", "pi",
    ])

    assert rc == 0
    cfg = AgentConfig.load("pi-agent")
    assert cfg.runtime.harness == "pi"
    assert cfg.runtime.harness_command == []
