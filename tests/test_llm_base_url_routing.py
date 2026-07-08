"""LiteLLM VK LLM-plane routing (Item B): a config-driven
``runtime.llm_base_url`` must thread into adapter/provider construction
so cloud agents' model calls hit the virtual-key endpoint, while an
absent/empty base URL leaves today's vendor-endpoint behavior unchanged.

Covers the runtime kinds the cloud agent uses:
  - chat-local: Anthropic + OpenAI provider ``client.base_url``
  - cli-local:  the claude-code spawn-env override (``_llm_env``)
  - sdk-local:  build_adapter threads ``base_url`` into SDKAdapter, and
    the shared env helper maps it to ANTHROPIC_BASE_URL — asserted
    without importing the optional ``claude-agent-sdk``.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.base import anthropic_base_url_env
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeConfig
from puffo_agent.portal.worker import build_adapter

VK = "https://vk.shan.example/litellm"


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    """Keep every adapter/config path under a throwaway home so building
    adapters never touches the real ~/.puffo-agent."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))


def _daemon_cfg() -> DaemonConfig:
    return DaemonConfig()


# ── chat-local: provider client.base_url ────────────────────────────────


def test_chat_local_anthropic_routes_through_vk():
    cfg = AgentConfig(
        id="chat-anth-vk",
        runtime=RuntimeConfig(
            kind="chat-local", provider="anthropic",
            api_key="k", llm_base_url=VK,
        ),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    base = str(adapter._provider.client.base_url)
    # httpx normalizes with a trailing slash; compare on the stem.
    assert base.rstrip("/") == VK.rstrip("/")


def test_chat_local_anthropic_default_when_unset():
    cfg = AgentConfig(
        id="chat-anth-default",
        runtime=RuntimeConfig(kind="chat-local", provider="anthropic", api_key="k"),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    base = str(adapter._provider.client.base_url)
    # Vendor default, byte-for-byte as before — and definitely not the VK.
    assert "anthropic.com" in base
    assert "vk.shan.example" not in base


def test_chat_local_openai_routes_through_vk():
    cfg = AgentConfig(
        id="chat-oai-vk",
        runtime=RuntimeConfig(
            kind="chat-local", provider="openai",
            api_key="k", llm_base_url=VK,
        ),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    base = str(adapter._provider.client.base_url)
    assert base.rstrip("/") == VK.rstrip("/")


def test_chat_local_openai_default_when_unset():
    cfg = AgentConfig(
        id="chat-oai-default",
        runtime=RuntimeConfig(kind="chat-local", provider="openai", api_key="k"),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    base = str(adapter._provider.client.base_url)
    assert "openai.com" in base
    assert "vk.shan.example" not in base


# ── cli-local: the claude-code spawn-env override ───────────────────────


def test_cli_local_env_override_carries_base_url_when_set():
    cfg = AgentConfig(
        id="cli-vk",
        runtime=RuntimeConfig(
            kind="cli-local", api_key="vk-secret", llm_base_url=VK,
        ),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    assert adapter.llm_base_url == VK
    env = adapter._llm_env()
    assert env["ANTHROPIC_BASE_URL"] == VK
    # The VK rides on runtime.api_key so the CLI authenticates to it.
    assert env["ANTHROPIC_API_KEY"] == "vk-secret"


def test_cli_local_env_override_empty_when_unset():
    cfg = AgentConfig(id="cli-default", runtime=RuntimeConfig(kind="cli-local"))
    adapter = build_adapter(_daemon_cfg(), cfg)
    # Empty base URL -> no env override at all, so the spawn env is
    # unchanged and claude keeps its ~/.claude / OAuth credential path.
    assert adapter._llm_env() == {}


def test_cli_local_no_api_key_injection_without_base_url():
    """A stray runtime.api_key alone (no base URL) must NOT leak an
    ANTHROPIC_API_KEY into the spawn env — that would silently override
    the operator's OAuth login."""
    cfg = AgentConfig(
        id="cli-key-only",
        runtime=RuntimeConfig(kind="cli-local", api_key="stray"),
    )
    adapter = build_adapter(_daemon_cfg(), cfg)
    assert adapter._llm_env() == {}


# ── sdk-local: wiring + shared env mapping (no claude-agent-sdk needed) ──


def test_sdk_local_build_adapter_threads_base_url(monkeypatch):
    """build_adapter must pass ``base_url`` into SDKAdapter. Stub the
    adapter so this holds whether or not the optional SDK is installed."""
    from puffo_agent.agent.adapters import sdk as sdk_mod

    captured: dict = {}

    class _StubSDKAdapter:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(sdk_mod, "SDKAdapter", _StubSDKAdapter)

    cfg = AgentConfig(
        id="sdk-vk",
        runtime=RuntimeConfig(kind="sdk-local", api_key="k", llm_base_url=VK),
    )
    build_adapter(_daemon_cfg(), cfg)
    assert captured.get("base_url") == VK


def test_sdk_local_build_adapter_base_url_empty_when_unset(monkeypatch):
    from puffo_agent.agent.adapters import sdk as sdk_mod

    captured: dict = {}

    class _StubSDKAdapter:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(sdk_mod, "SDKAdapter", _StubSDKAdapter)

    cfg = AgentConfig(
        id="sdk-default",
        runtime=RuntimeConfig(kind="sdk-local", api_key="k"),
    )
    build_adapter(_daemon_cfg(), cfg)
    # Empty threads through as "" -> SDKAdapter.run_turn injects nothing.
    assert captured.get("base_url") == ""


def test_shared_env_helper_maps_base_url():
    """The SDK adapter's run_turn env and the cli adapter's _llm_env both
    build on this helper; it must map a set base URL to ANTHROPIC_BASE_URL
    and an empty one to no override."""
    assert anthropic_base_url_env(VK) == {"ANTHROPIC_BASE_URL": VK}
    assert anthropic_base_url_env("") == {}
    assert anthropic_base_url_env(None) == {}  # type: ignore[arg-type]


# ── config round-trip (RuntimeConfig.llm_base_url) ──────────────────────


def test_runtime_config_llm_base_url_round_trips():
    """agent.yml save/load preserves llm_base_url (it serializes via
    asdict(runtime), so this guards the load-side parse)."""
    cfg = AgentConfig(
        id="rt-agent",
        runtime=RuntimeConfig(kind="chat-local", api_key="k", llm_base_url=VK),
    )
    cfg.save()
    reloaded = AgentConfig.load("rt-agent")
    assert reloaded.runtime.llm_base_url == VK
