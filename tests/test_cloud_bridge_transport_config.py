"""T23 phase 1: ``puffo_core.transport`` config parse / validate /
round-trip. The flag-off (native) path must stay byte-identical:
no new keys in saved agent.yml, every parsed field unchanged.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _write_agent_yml(agent_id: str, puffo_core_extra: str = "") -> Path:
    from puffo_agent.portal.state import agent_yml_path
    yml = agent_yml_path(agent_id)
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text(
        f"id: {agent_id}\n"
        "state: running\n"
        f"display_name: {agent_id}\n"
        "created_at: 0\n"
        "puffo_core:\n"
        "  server_url: 'https://relay.example'\n"
        "  slug: 'bot-1234'\n"
        "  device_id: 'dev-1'\n"
        "  space_id: 'sp-1'\n"
        "  operator_slug: 'op-5678'\n"
        f"{puffo_core_extra}"
        "runtime: {kind: chat-local}\n"
        "triggers: {on_mention: true, on_dm: true}\n",
        encoding="utf-8",
    )
    return yml


def test_absent_transport_key_defaults_to_native(home):
    from puffo_agent.portal.state import AgentConfig
    _write_agent_yml("agent-no-transport")
    cfg = AgentConfig.load("agent-no-transport")
    assert cfg.puffo_core.transport == "native"
    assert cfg.puffo_core.sandbox_token == ""


def test_absent_transport_key_leaves_every_other_field_unchanged(home):
    # Control: an identical agent.yml under a different id. Loading
    # the transport-less file must parse every non-transport field
    # exactly as the control does.
    from puffo_agent.portal.state import AgentConfig
    _write_agent_yml("agent-a")
    _write_agent_yml("agent-b")
    a = asdict(AgentConfig.load("agent-a").puffo_core)
    b = asdict(AgentConfig.load("agent-b").puffo_core)
    assert a == b
    assert a["server_url"] == "https://relay.example"
    assert a["slug"] == "bot-1234"
    assert a["device_id"] == "dev-1"
    assert a["space_id"] == "sp-1"
    assert a["operator_slug"] == "op-5678"
    assert a["auto_accept_space_invitations"] is False


def test_native_save_emits_no_transport_keys(home):
    # Flag-off byte-identity: the saved puffo_core block carries the
    # exact pre-T23 key-set — transport/sandbox_token never leak in.
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    cfg = AgentConfig(id="agent-native-save", display_name="n")
    cfg.puffo_core.slug = "bot-1234"
    cfg.save()
    raw = yaml.safe_load(
        agent_yml_path("agent-native-save").read_text(encoding="utf-8")
    )
    assert set(raw["puffo_core"].keys()) == {
        "server_url",
        "slug",
        "device_id",
        "space_id",
        "operator_slug",
        "auto_accept_space_invitations",
    }


def test_bridge_fields_parse_and_round_trip(home):
    from puffo_agent.portal.state import AgentConfig, agent_yml_path
    _write_agent_yml(
        "agent-bridge",
        puffo_core_extra=(
            "  transport: bridge\n"
            "  sandbox_token: 'sbx_secret_1'\n"
        ),
    )
    cfg = AgentConfig.load("agent-bridge")
    assert cfg.puffo_core.transport == "bridge"
    assert cfg.puffo_core.sandbox_token == "sbx_secret_1"
    assert cfg.puffo_core.server_url == "https://relay.example"
    # Bridge agents DO persist the transport keys.
    cfg.save()
    raw = yaml.safe_load(
        agent_yml_path("agent-bridge").read_text(encoding="utf-8")
    )
    assert raw["puffo_core"]["transport"] == "bridge"
    assert raw["puffo_core"]["sandbox_token"] == "sbx_secret_1"
    reloaded = AgentConfig.load("agent-bridge")
    assert reloaded.puffo_core.transport == "bridge"
    assert reloaded.puffo_core.sandbox_token == "sbx_secret_1"


def test_bridge_without_sandbox_token_fails_fast(home):
    from puffo_agent.portal.state import AgentConfig
    _write_agent_yml(
        "agent-bridge-no-token",
        puffo_core_extra="  transport: bridge\n",
    )
    with pytest.raises(RuntimeError, match="sandbox_token"):
        AgentConfig.load("agent-bridge-no-token")


def test_unknown_transport_fails_fast_naming_valid_transports(home):
    from puffo_agent.portal.state import AgentConfig
    _write_agent_yml(
        "agent-bad-transport",
        puffo_core_extra="  transport: carrier-pigeon\n",
    )
    with pytest.raises(RuntimeError, match="native.*bridge"):
        AgentConfig.load("agent-bad-transport")
