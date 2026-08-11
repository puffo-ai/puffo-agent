"""Compatibility checks for agent.yml files written by older releases."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _write_agent(agent_id: str, body: dict) -> None:
    from puffo_agent.portal.state import agent_yml_path

    path = agent_yml_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def test_legacy_agent_config_load_save_preserves_existing_values(home):
    """Missing new fields get defaults without losing legacy settings."""
    from puffo_agent.portal.state import AgentConfig, agent_yml_path

    agent_id = "legacy-agent"
    _write_agent(agent_id, {
        "id": agent_id,
        "state": "paused",
        "display_name": "Legacy Agent",
        "avatar_url": "https://cdn.example/avatar.png",
        "role": "Keeps the old workspace running",
        "role_short": "Keeper",
        "created_at": 123456789,
        "puffo_core": {
            "server_url": "https://api.example",
            "slug": "legacy-1234",
            "device_id": "device-old",
            "space_id": "space-old",
            "operator_slug": "owner-5678",
        },
        "runtime": {
            "kind": "chat-only",
            "provider": "anthropic",
            "model": "legacy-model",
            "api_key": "fake-old-key",
            "allowed_tools": ["Read", "Bash(git status)"],
            "permission_mode": "bypassPermissions",
            "harness": "claude-code",
            "max_turns": 23,
        },
        "profile": "identity/profile.md",
        "memory_dir": "memory-old",
        "workspace_dir": "workspace-old",
        "triggers": {"on_mention": False, "on_dm": True},
    })

    loaded = AgentConfig.load(agent_id)
    assert loaded.runtime.kind == "cli-local"
    assert loaded.runtime.task_timeout_seconds == 1800.0
    loaded.save()

    if os.name != "nt":
        assert agent_yml_path(agent_id).parent.stat().st_mode & 0o777 == 0o700
        assert agent_yml_path(agent_id).stat().st_mode & 0o777 == 0o600

    saved = yaml.safe_load(agent_yml_path(agent_id).read_text(encoding="utf-8"))
    assert saved["state"] == "paused"
    assert saved["display_name"] == "Legacy Agent"
    assert saved["avatar_url"] == "https://cdn.example/avatar.png"
    assert saved["role"] == "Keeps the old workspace running"
    assert saved["role_short"] == "Keeper"
    assert saved["created_at"] == 123456789
    assert saved["puffo_core"]["server_url"] == "https://api.example"
    assert saved["puffo_core"]["slug"] == "legacy-1234"
    assert saved["puffo_core"]["device_id"] == "device-old"
    assert saved["puffo_core"]["space_id"] == "space-old"
    assert saved["puffo_core"]["operator_slug"] == "owner-5678"
    assert saved["runtime"]["kind"] == "cli-local"
    assert saved["runtime"]["provider"] == "anthropic"
    assert saved["runtime"]["model"] == "legacy-model"
    assert saved["runtime"]["api_key"] == "fake-old-key"
    assert saved["runtime"]["allowed_tools"] == ["Read", "Bash(git status)"]
    assert saved["runtime"]["max_turns"] == 23
    assert saved["profile"] == "identity/profile.md"
    assert saved["memory_dir"] == "memory-old"
    assert saved["workspace_dir"] == "workspace-old"
    assert saved["triggers"] == {"on_mention": False, "on_dm": True}


@pytest.mark.parametrize(("runtime", "expected", "error"), [
    # Pre-Driver ``cli-local`` + ``hermes`` migrates to the resolved harness.
    ({"kind": "cli-local", "harness": "hermes"},
     {"harness": "claude-code", "provider": ""}, None),
    ({"kind": "cli-local", "provider": "anthropic", "harness": "hermes"},
     {"harness": "claude-code", "provider": "anthropic"}, None),
    ({"kind": "cli-local", "provider": "openai", "harness": "hermes"},
     {"harness": "codex", "provider": "openai"}, None),
    # A level the migrated harness cannot serve is dropped; a valid one stays.
    ({"kind": "cli-local", "provider": "anthropic", "harness": "hermes",
      "inference_level": "minimal"},
     {"harness": "claude-code", "inference_level": ""}, None),
    ({"kind": "cli-local", "provider": "openai", "harness": "hermes",
      "inference_level": "medium"},
     {"harness": "codex", "inference_level": "medium"}, None),
    # Legacy kinds keep migrating, resolving the harness from the provider.
    ({"kind": "sdk-local", "provider": "openai"},
     {"kind": "cli-local", "harness": "codex"}, None),
    # Migration is scoped to cli-local, and Docker hermes is no longer
    # supported: a persisted agent.yml fails at load with one explicit
    # diagnostic, before any worker is constructed.
    ({"kind": "cli-docker", "provider": "openai", "harness": "hermes"},
     None, "design-only"),
    # No Driver harness serves google, so this stays an explicit load error.
    ({"kind": "cli-local", "provider": "google", "harness": "gemini-cli"},
     None, "not implemented by the Driver runtime"),
])
def test_stale_hermes_harness_migrates_to_driver_harness(
    home, runtime, expected, error,
):
    """``cli-local`` + ``hermes`` was valid before the Driver runtime landed.

    Such configs must migrate to the provider-resolved harness rather than
    leave the agent unloadable; only combinations with no valid Driver harness
    keep raising.
    """
    from puffo_agent.portal.state import AgentConfig

    agent_id = "stale-harness"
    _write_agent(agent_id, {"id": agent_id, "runtime": runtime})

    if error is not None:
        with pytest.raises(RuntimeError, match=error):
            AgentConfig.load(agent_id)
        return

    loaded = AgentConfig.load(agent_id)
    for field, value in expected.items():
        assert getattr(loaded.runtime, field) == value, field


def test_task_timeout_seconds_round_trips(home):
    from puffo_agent.portal.state import AgentConfig

    agent_id = "custom-timeout"
    _write_agent(agent_id, {
        "id": agent_id,
        "runtime": {
            "kind": "cli-local",
            "provider": "openai",
            "harness": "codex",
            "task_timeout_seconds": 123.5,
        },
    })

    loaded = AgentConfig.load(agent_id)
    assert loaded.runtime.task_timeout_seconds == 123.5
    loaded.save()
    assert AgentConfig.load(agent_id).runtime.task_timeout_seconds == 123.5
