"""LiteLLM VK LLM-plane routing (Item B): a config-driven
``runtime.llm_base_url`` must thread into Driver construction so cloud
agents' model calls hit the virtual-key endpoint, while an
absent/empty base URL leaves today's vendor-endpoint behavior unchanged.

Covers the cloud Agent's cli-local Claude Driver and the shared base-URL
environment mapping.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.base import anthropic_base_url_env
from puffo_agent.agent.harness.runtime.local_runtime import LocalRuntimePreparer
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig

VK = "https://vk.shan.example/litellm"


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    """Keep every adapter/config path under a throwaway home so building
    adapters never touches the real ~/.puffo-agent."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))


def _daemon_cfg() -> DaemonConfig:
    return DaemonConfig()


# ── cli-local: the claude-code spawn-env override ───────────────────────


def _local_claude_spec(cfg, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime, "resolve_claude_bin", lambda: "/opt/bin/claude"
    )
    return LocalRuntimePreparer(_daemon_cfg(), cfg)._prepare_claude_spec("")


def test_cli_local_env_override_carries_base_url_when_set(monkeypatch):
    cfg = AgentConfig(
        id="cli-vk",
        runtime=RuntimeConfig(
            kind="cli-local", api_key="vk-secret", llm_base_url=VK,
        ),
    )
    env = _local_claude_spec(cfg, monkeypatch).environment
    assert env["ANTHROPIC_BASE_URL"] == VK
    # The VK rides on runtime.api_key so the CLI authenticates to it.
    assert env["ANTHROPIC_API_KEY"] == "vk-secret"


def test_cli_local_env_override_empty_when_unset(monkeypatch):
    cfg = AgentConfig(id="cli-default", runtime=RuntimeConfig(kind="cli-local"))
    # Empty base URL -> no env override at all, so the spawn env is
    # unchanged and claude keeps its ~/.claude / OAuth credential path.
    env = _local_claude_spec(cfg, monkeypatch).environment
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_cli_local_no_api_key_injection_without_base_url(monkeypatch):
    """A stray runtime.api_key alone (no base URL) must NOT leak an
    ANTHROPIC_API_KEY into the spawn env — that would silently override
    the operator's OAuth login."""
    cfg = AgentConfig(
        id="cli-key-only",
        runtime=RuntimeConfig(kind="cli-local", api_key="stray"),
    )
    env = _local_claude_spec(cfg, monkeypatch).environment
    assert "ANTHROPIC_API_KEY" not in env


def test_shared_env_helper_maps_base_url():
    """A set base URL maps to ANTHROPIC_BASE_URL; empty stays inert."""
    assert anthropic_base_url_env(VK) == {"ANTHROPIC_BASE_URL": VK}
    assert anthropic_base_url_env("") == {}
    assert anthropic_base_url_env(None) == {}  # type: ignore[arg-type]


# ── config round-trip (RuntimeConfig.llm_base_url) ──────────────────────


def test_runtime_config_llm_base_url_round_trips():
    """agent.yml save/load preserves llm_base_url (it serializes via
    asdict(runtime), so this guards the load-side parse)."""
    cfg = AgentConfig(
        id="rt-agent",
        runtime=RuntimeConfig(kind="cli-local", api_key="k", llm_base_url=VK),
    )
    cfg.save()
    reloaded = AgentConfig.load("rt-agent")
    assert reloaded.runtime.llm_base_url == VK
