from __future__ import annotations

import json
from pathlib import Path

import pytest

from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter
from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
from puffo_agent.portal import cli, state
from puffo_agent.portal.daemon import Daemon
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    RuntimeConfig,
    RuntimeState,
)
from puffo_agent.portal.worker import Worker, build_docker_adapter


def _local_preparer(
    tmp_path: Path,
    monkeypatch,
    *,
    daemon_key: str = "",
    key_enabled: bool = False,
    runtime_key: str = "",
    base_url: str = "",
) -> LocalRuntimePreparer:
    import puffo_agent.agent.harness.local_runtime as local_runtime

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    monkeypatch.setattr(local_runtime, "resolve_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    daemon = DaemonConfig()
    daemon.anthropic.api_key = daemon_key
    daemon.anthropic.cli_use_api_key = key_enabled
    config = AgentConfig(
        id="key-policy",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider="anthropic",
            harness="claude-code",
            api_key=runtime_key,
            llm_base_url=base_url,
        ),
    )
    return LocalRuntimePreparer(daemon, config)


def _docker_adapter(tmp_path: Path, *, api_key: str = "") -> DockerCLIAdapter:
    return DockerCLIAdapter(
        agent_id="docker-key-policy",
        model="claude-sonnet-5",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "workspace" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "agent-home"),
        shared_fs_dir=str(tmp_path / "shared"),
        claude_api_key=api_key,
    )


def test_daemon_anthropic_cli_api_key_opt_in_round_trips(tmp_path, monkeypatch):
    config_path = tmp_path / "daemon.yml"
    monkeypatch.setattr(state, "daemon_yml_path", lambda: config_path)
    config = DaemonConfig()
    config.anthropic.api_key = "daemon-key"
    config.anthropic.cli_use_api_key = True

    config.save()

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "daemon-key"
    assert loaded.anthropic.cli_use_api_key is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), (False, False), ("true", False), (True, True)],
)
def test_daemon_cli_api_key_requires_yaml_boolean_true(
    tmp_path, monkeypatch, raw, expected
):
    config_path = tmp_path / "daemon.yml"
    monkeypatch.setattr(state, "daemon_yml_path", lambda: config_path)
    anthropic = {"api_key": "daemon-key"}
    if raw is not None:
        anthropic["cli_use_api_key"] = raw
    config_path.write_text(
        "anthropic:\n"
        + "".join(
            f"  {key}: {json.dumps(value)}\n" for key, value in anthropic.items()
        ),
        encoding="utf-8",
    )

    assert DaemonConfig.load().anthropic.cli_use_api_key is expected


def test_config_command_sets_explicit_api_key_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    assert cli.main([
        "config",
        "--anthropic-api-key",
        "configured-key",
        "--anthropic-cli-use-api-key",
        "true",
    ]) == 0

    loaded = DaemonConfig.load()
    assert loaded.anthropic.api_key == "configured-key"
    assert loaded.anthropic.cli_use_api_key is True


def test_settings_scrubber_removes_only_anthropic_key(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "stale", "KEEP": "value"}}),
        encoding="utf-8",
    )

    assert state.strip_claude_api_key_from_settings(path) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "env": {"KEEP": "value"}
    }


def test_local_driver_ignores_ambient_key_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    preparer = _local_preparer(tmp_path, monkeypatch)

    spec = preparer._prepare_claude_spec("prompt")

    assert "ANTHROPIC_API_KEY" not in spec.environment


def test_local_driver_uses_explicit_daemon_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    preparer = _local_preparer(
        tmp_path,
        monkeypatch,
        daemon_key="daemon-key",
        key_enabled=True,
    )

    spec = preparer._prepare_claude_spec("prompt")

    assert spec.environment["ANTHROPIC_API_KEY"] == "daemon-key"


def test_local_gateway_key_takes_precedence(tmp_path, monkeypatch):
    preparer = _local_preparer(
        tmp_path,
        monkeypatch,
        daemon_key="daemon-key",
        key_enabled=True,
        runtime_key="gateway-key",
        base_url="https://gateway.example/v1",
    )

    spec = preparer._prepare_claude_spec("prompt")

    assert spec.environment["ANTHROPIC_API_KEY"] == "gateway-key"
    assert spec.environment["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1"


def test_docker_key_is_passed_by_name_not_argv_value(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    adapter = _docker_adapter(tmp_path, api_key="daemon-key")
    adapter._prepare_mcp_args = lambda: []

    session = adapter._ensure_session()
    command = session.build_command([], {})

    assert session.env["ANTHROPIC_API_KEY"] == "daemon-key"
    assert command[:5] == [
        "docker",
        "exec",
        "-i",
        "-e",
        "ANTHROPIC_API_KEY",
    ]
    assert "daemon-key" not in command


def test_worker_applies_key_only_to_claude_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    config = AgentConfig(
        id="docker-key-policy",
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="anthropic",
            harness="claude-code",
        ),
    )
    daemon = DaemonConfig()
    daemon.anthropic.api_key = "daemon-key"
    daemon.anthropic.cli_use_api_key = True

    adapter = build_docker_adapter(daemon, config)

    assert adapter.claude_api_key == "daemon-key"


def test_daemon_skips_oauth_refresher_for_explicit_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    config = AgentConfig(
        id="refresh-policy",
        runtime=RuntimeConfig(kind="cli-local", harness="claude-code"),
    )
    daemon_config = DaemonConfig()
    daemon_config.anthropic.api_key = "daemon-key"
    daemon_config.anthropic.cli_use_api_key = True
    daemon = Daemon(daemon_config)

    assert daemon._notify_refresh_for(config) is None
    assert daemon._ensure_fresh_for(config) is None


def test_successful_key_retry_clears_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    config = AgentConfig(
        id="key-recovery",
        runtime=RuntimeConfig(kind="cli-local", harness="claude-code"),
    )
    daemon = DaemonConfig()
    daemon.anthropic.api_key = "daemon-key"
    daemon.anthropic.cli_use_api_key = True
    worker = Worker(daemon, config)
    worker.runtime = RuntimeState(status="running", health="auth_failed")
    worker._api_key_auth_recovery_pending = True
    worker._auth_failed_notification_sent = True

    worker._resolve_health_after_success(config.id)

    assert worker.runtime.health == "ok"
    assert worker._api_key_auth_recovery_pending is False
    assert worker._auth_failed_notification_sent is False
