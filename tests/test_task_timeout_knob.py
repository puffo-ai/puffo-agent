"""RuntimeConfig.task_timeout_seconds round-trips through agent.yml,
defaults to 600s, and reaches the CodexSession."""

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


def test_local_cli_adapter_plumbs_timeout_to_codex_session(tmp_path, monkeypatch):
    from puffo_agent.agent.adapters import local_cli as lc
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
    from puffo_agent.agent.harness import CodexHarness

    host_home = tmp_path / "host"
    host_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host_home))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(lc, "is_macos", lambda: False)
    monkeypatch.setattr(lc, "sync_host_codex_auth_view", lambda *a, **k: "shared")
    monkeypatch.setattr(lc, "resolve_codex_bin", lambda: str(tmp_path / "codex"))

    captured: dict = {}

    class _Capture:
        def __init__(self, *a, **k):
            captured.update(k)
            raise RuntimeError("stop before spawn")

    monkeypatch.setattr(lc, "CodexSession", _Capture)
    adapter = LocalCLIAdapter(
        agent_id="a",
        model="",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "cl"),
        session_file=str(tmp_path / "s.json"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        agent_home_dir=str(tmp_path / "agents" / "a"),
        harness=CodexHarness(),
        permission_mode="bypassPermissions",
        task_timeout_seconds=123.0,
    )
    with pytest.raises(RuntimeError, match="stop before spawn"):
        adapter._ensure_codex_session()
    assert captured["task_timeout_seconds"] == 123.0


def test_timeout_budget_label():
    from puffo_agent.agent.adapters.codex_session import _timeout_budget_label
    assert _timeout_budget_label(600.0) == "10-minute"
    assert _timeout_budget_label(1800.0) == "30-minute"
    assert _timeout_budget_label(90.0) == "1-minute"
    assert _timeout_budget_label(45.0) == "45-second"
    assert _timeout_budget_label(5.0) == "5-second"
