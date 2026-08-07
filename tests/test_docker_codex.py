from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from puffo_agent.agent.adapters import docker_cli
from puffo_agent.agent.adapters.docker_cli import DOCKERFILE, DockerCLIAdapter
from puffo_agent.agent.harness import CodexHarness, GeminiCLIHarness, HermesHarness


def _adapter(tmp_path: Path, *, harness=None, **overrides) -> DockerCLIAdapter:
    kwargs = {
        "agent_id": "docker-codex",
        "model": "gpt-5.4",
        "image": "puffo/agent-runtime:test",
        "workspace_dir": str(tmp_path / "workspace"),
        "claude_dir": str(tmp_path / "workspace" / ".claude"),
        "session_file": str(tmp_path / "cli_session.json"),
        "agent_home_dir": str(tmp_path / "agent-home"),
        "shared_fs_dir": str(tmp_path / "shared"),
        "harness": harness,
    }
    kwargs.update(overrides)
    return DockerCLIAdapter(**kwargs)


def test_bundled_image_contains_only_supported_harnesses():
    assert "@anthropic-ai/claude-code@" in DOCKERFILE
    assert "@openai/codex@" in DOCKERFILE
    assert '"mcp>=1.0,<2"' in DOCKERFILE
    assert '"aiohttp-socks>=0.10"' in DOCKERFILE
    assert '"psutil>=5.9"' in DOCKERFILE
    assert "@google/gemini-cli" not in DOCKERFILE
    assert "hermes-agent" not in DOCKERFILE


@pytest.mark.parametrize("harness", [GeminiCLIHarness(), HermesHarness()])
def test_adapter_rejects_removed_docker_harnesses(tmp_path, harness):
    with pytest.raises(RuntimeError, match="supports only claude-code and codex"):
        _adapter(tmp_path, harness=harness)


def test_container_mounts_per_agent_codex_and_sanitized_claude_homes(tmp_path):
    host_home = tmp_path / "host"
    host_home.mkdir()
    adapter = _adapter(tmp_path, harness=CodexHarness())
    captured: list[list[str]] = []

    async def fake_run(cmd, check=True):
        captured.append(list(cmd))
        return 0, b"", b""

    with patch.object(docker_cli, "_run_cmd", new=fake_run), patch.object(
        docker_cli.Path, "home", staticmethod(lambda: host_home),
    ):
        asyncio.run(adapter._start_container())

    argv = next(cmd for cmd in captured if cmd[:2] == ["docker", "run"])
    assert f"{adapter.codex_home}:/home/agent/.codex" in argv
    assert f"{adapter.claude_home_src}:/home/agent/.claude" in argv
    assert not any(
        str(arg).endswith(":/home/agent/.claude/.credentials.json")
        for arg in argv
    )
    assert not any("/.gemini" in str(arg) for arg in argv)


def test_prepare_codex_config_syncs_auth_and_container_reachable_mcps(tmp_path):
    host_home = tmp_path / "host"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text(
        json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "access",
                "refresh_token": "refresh-secret",
            },
        }),
        encoding="utf-8",
    )
    (host_codex / "config.toml").write_text(
        "\n".join([
            "[mcp_servers.reachable]",
            'command = "npx"',
            'args = ["-y", "server"]',
            "",
            "[mcp_servers.host_only]",
            'command = "/Users/operator/bin/server"',
        ]),
        encoding="utf-8",
    )

    adapter = _adapter(tmp_path, harness=CodexHarness(), inference_level="high")
    adapter._desired_codex_extras = {
        "desired": {"command": "uvx", "args": ["desired-server"], "env": {}},
    }
    adapter.puffo_core_mcp_env = {"PUFFO_CORE_SLUG": "agent-slug"}
    adapter._prepare_codex_config(host_home)

    auth = json.loads((adapter.codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["tokens"]["refresh_token"] == ""
    config = (adapter.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in config
    assert 'model_reasoning_effort = "high"' in config
    assert "[mcp_servers.reachable]" in config
    assert "[mcp_servers.desired]" in config
    assert "[mcp_servers.puffo]" in config
    assert "host_only" not in config
    assert 'PUFFO_WORKSPACE = "/workspace"' in config
    assert 'PUFFO_HARNESS = "codex"' in config


def test_container_codex_mcp_reads_container_mounted_codex_home(tmp_path):
    adapter = _adapter(tmp_path, harness=CodexHarness())
    adapter.puffo_core_mcp_env = {
        "CODEX_HOME": str(adapter.codex_home),
        "PUFFO_CORE_SLUG": "agent-slug",
    }

    env = adapter._container_puffo_mcp_env()

    assert env is not None
    assert env["CODEX_HOME"] == "/home/agent/.codex"


def test_prepare_codex_config_uses_container_codex_home_for_puffo_mcp(tmp_path):
    host_home = tmp_path / "host"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text("{}", encoding="utf-8")
    adapter = _adapter(tmp_path, harness=CodexHarness())
    adapter.puffo_core_mcp_env = {
        "CODEX_HOME": str(adapter.codex_home),
        "PUFFO_CORE_SLUG": "agent-slug",
    }

    adapter._prepare_codex_config(host_home)

    config = (adapter.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'CODEX_HOME = "/home/agent/.codex"' in config
    assert str(adapter.codex_home).replace("\\", "\\\\") not in config


def test_codex_session_runs_app_server_inside_container(tmp_path):
    adapter = _adapter(
        tmp_path,
        harness=CodexHarness(),
        permission_mode="bypassPermissions",
        sandbox="workspace-write",
        task_timeout_seconds=321,
    )
    session = adapter._ensure_codex_session()
    assert session.argv == [
        "docker", "exec", "-i",
        "-e", "CODEX_HOME=/home/agent/.codex",
        "puffo-docker-codex", "codex", "app-server",
    ]
    assert session.cwd is None
    assert session.thread_cwd == "/workspace"
    assert session.sandbox == "workspace-write"
    assert session.task_timeout_seconds == 321
    assert session.model == "gpt-5.4"


def test_resolved_docker_path_is_used_for_harness_commands(tmp_path):
    docker_bin = r"C:\Docker Desktop\resources\bin\docker.exe"

    claude_adapter = _adapter(tmp_path)
    claude_adapter._docker_bin = docker_bin
    assert claude_adapter._build_command([])[0] == docker_bin

    codex_adapter = _adapter(tmp_path, harness=CodexHarness())
    codex_adapter._docker_bin = docker_bin
    assert codex_adapter._ensure_codex_session().argv[0] == docker_bin


def test_ensure_started_fails_when_docker_resolver_misses(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(docker_cli, "resolve_docker_bin", lambda: None)

    with pytest.raises(RuntimeError, match="PUFFO_DOCKER_BIN"):
        asyncio.run(adapter._ensure_started())


@pytest.mark.parametrize("token_value, forwarded", [
    ("super-secret", True),
    (None, False),
])
def test_codex_session_forwards_only_available_bearer_env(
    tmp_path, monkeypatch, token_value, forwarded,
):
    host_home = tmp_path / "host"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": "refresh-secret"}}),
        encoding="utf-8",
    )
    (host_codex / "config.toml").write_text(
        "\n".join([
            "[mcp_servers.secure]",
            'url = "https://mcp.example.test"',
            'bearer_token_env_var = "SECURE_MCP_TOKEN"',
        ]),
        encoding="utf-8",
    )
    if token_value is not None:
        monkeypatch.setenv("SECURE_MCP_TOKEN", token_value)
    else:
        monkeypatch.delenv("SECURE_MCP_TOKEN", raising=False)
    adapter = _adapter(tmp_path, harness=CodexHarness())

    adapter._prepare_codex_config(host_home)
    argv = adapter._ensure_codex_session().argv

    assert ("SECURE_MCP_TOKEN" in argv) is forwarded
    if forwarded:
        token_flag = argv.index("SECURE_MCP_TOKEN")
        assert argv[token_flag - 1] == "-e"
    assert "super-secret" not in argv


def test_worker_uses_openai_defaults_and_forwards_desired_content(monkeypatch, tmp_path):
    from puffo_agent.portal.worker import build_adapter

    captured: dict = {}

    class StubAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(docker_cli, "DockerCLIAdapter", StubAdapter)
    runtime = SimpleNamespace(
        kind="cli-docker",
        harness="codex",
        model="",
        docker_image="",
        docker_memory_limit="",
        docker_memory_reservation="",
        permission_mode="bypassPermissions",
        sandbox="workspace-write",
        inference_level="high",
        task_timeout_seconds=456,
    )
    puffo_core = SimpleNamespace(
        server_url="", slug="", device_id="", space_id="",
        is_configured=lambda: False,
    )
    agent = SimpleNamespace(
        id="agent-codex",
        runtime=runtime,
        desired_skills=["review"],
        desired_mcps=["github"],
        env_overrides={},
        puffo_core=puffo_core,
        resolve_workspace_dir=lambda: tmp_path / "workspace",
        resolve_claude_dir=lambda: tmp_path / "workspace" / ".claude",
    )
    daemon = SimpleNamespace(
        anthropic=SimpleNamespace(model="claude-default"),
        openai=SimpleNamespace(model="gpt-default"),
        docker_memory_limit="1g",
        docker_memory_reservation="256m",
        data_service=SimpleNamespace(port=63388),
        rpc_service=SimpleNamespace(port=63389),
    )

    build_adapter(daemon, agent)

    assert captured["model"] == "gpt-default"
    assert captured["desired_skills"] == ["review"]
    assert captured["desired_mcps"] == ["github"]
    assert captured["sandbox"] == "workspace-write"
    assert captured["task_timeout_seconds"] == 456
