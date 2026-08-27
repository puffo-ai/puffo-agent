"""The task timeout persists and reaches the Driver runtime boundary."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def test_default_is_1800():
    from puffo_agent.portal.state import RuntimeConfig
    assert RuntimeConfig().task_timeout_seconds == 1800.0


def test_round_trips(home):
    from puffo_agent.portal.state import AgentConfig, RuntimeConfig
    cfg = AgentConfig(
        id="codex-slow",
        display_name="codex-slow",
        runtime=RuntimeConfig(kind="cli-local", harness="codex", task_timeout_seconds=900.0),
    )
    cfg.save()
    assert AgentConfig.load("codex-slow").runtime.task_timeout_seconds == 900.0


def test_legacy_yml_without_field_defaults_1800(home):
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
    assert AgentConfig.load(aid).runtime.task_timeout_seconds == 1800.0


def test_runtime_spec_default_matches_config_default():
    from puffo_agent.agent.harness.driver import RuntimeSpec

    assert RuntimeSpec("/tmp").task_timeout_seconds == 1800.0


@pytest.mark.parametrize(
    ("provider", "harness", "resolver_name"),
    [
        ("anthropic", "claude-code", "resolve_claude_bin"),
        ("openai", "codex", "resolve_codex_bin"),
    ],
)
def test_local_runtime_spec_receives_configured_timeout(
    home, monkeypatch, provider, harness, resolver_name,
):
    import puffo_agent.agent.harness.local_runtime as local_runtime
    from puffo_agent.agent.harness.local_runtime import LocalRuntimePreparer
    from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig

    monkeypatch.setattr(local_runtime, resolver_name, lambda: "/bin/runtime")
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    monkeypatch.setattr(
        local_runtime, "sync_host_codex_auth_view", lambda *_args: "view",
    )
    cfg = AgentConfig(
        id=f"{harness}-timeout",
        runtime=RuntimeConfig(
            kind="cli-local",
            provider=provider,
            harness=harness,
            task_timeout_seconds=777.0,
        ),
    )
    preparer = LocalRuntimePreparer(DaemonConfig(), cfg)
    method = (
        preparer._prepare_codex_spec
        if harness == "codex"
        else preparer._prepare_claude_spec
    )

    assert method("prompt").task_timeout_seconds == 777.0
