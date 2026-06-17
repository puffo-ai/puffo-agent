"""cli-cloud runtime + Bridge seam (A1-A5)."""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.bridge import message_client as mc_mod
from puffo_agent.bridge.backup import is_backed_up
from puffo_agent.bridge.client import BridgeConfig, BridgeInboundEvent, StubBridgeClient
from puffo_agent.bridge.message_client import BridgeMessageClient
from puffo_agent.portal.runtime_matrix import (
    RUNTIME_CLI_CLOUD,
    VALID_RUNTIMES,
    harness_applies,
    validate_triple,
)


class _FakeStore:
    def __init__(self) -> None:
        self.stored: list[dict] = []

    async def store(self, payload, *, received_at=None) -> None:
        self.stored.append(payload)


# ── Stage 0: runtime registration ─────────────────────────────────────

def test_runtime_registered():
    assert RUNTIME_CLI_CLOUD == "cli-cloud"
    assert RUNTIME_CLI_CLOUD in VALID_RUNTIMES
    assert harness_applies("cli-cloud")
    assert validate_triple("cli-cloud", "anthropic", "claude-code").ok
    assert validate_triple("cli-cloud", "openai", "codex").ok
    # claude-code is anthropic-only — provider mismatch is rejected.
    assert not validate_triple("cli-cloud", "google", "claude-code").ok


# ── A5: backup manifest ───────────────────────────────────────────────

def test_backup_manifest():
    assert is_backed_up("memory/notes.md")
    assert is_backed_up("workspace/out/report.txt")
    assert not is_backed_up("keys/id.json")
    assert not is_backed_up("messages.db")
    assert not is_backed_up("workspace/.claude/.credentials.json")
    assert not is_backed_up("runtime.json")


# ── A3: late identity binding ─────────────────────────────────────────

def test_late_binding_fills_blanks(monkeypatch):
    from puffo_agent.portal.state import (
        AgentConfig,
        RuntimeConfig,
        _apply_cloud_late_binding,
    )

    monkeypatch.setenv("PUFFO_AGENT_ID", "ag1")
    monkeypatch.setenv("PUFFO_CORE_SLUG", "slug1")
    monkeypatch.setenv("PUFFO_CORE_SPACE_ID", "sp1")
    monkeypatch.setenv("PUFFO_BRIDGE_URL", "https://bridge")
    monkeypatch.setenv("PUFFO_LLM_GATEWAY_URL", "https://gw")

    cfg = AgentConfig(id="", runtime=RuntimeConfig(kind="cli-cloud"))
    _apply_cloud_late_binding(cfg)
    assert cfg.id == "ag1"
    assert cfg.puffo_core.slug == "slug1"
    assert cfg.puffo_core.space_id == "sp1"
    assert cfg.runtime.bridge_url == "https://bridge"
    assert cfg.runtime.llm_gateway_url == "https://gw"


def test_late_binding_baked_value_wins(monkeypatch):
    from puffo_agent.portal.state import (
        AgentConfig,
        RuntimeConfig,
        _apply_cloud_late_binding,
    )

    monkeypatch.setenv("PUFFO_AGENT_ID", "fromenv")
    monkeypatch.setenv("PUFFO_BRIDGE_URL", "https://env")
    cfg = AgentConfig(
        id="baked", runtime=RuntimeConfig(kind="cli-cloud", bridge_url="https://baked"),
    )
    _apply_cloud_late_binding(cfg)
    assert cfg.id == "baked"
    assert cfg.runtime.bridge_url == "https://baked"


# ── A4: LiteLLM gateway env ───────────────────────────────────────────

def _cloud_adapter(tmp_path, harness_name, **kw):
    from puffo_agent.agent.adapters.cli_cloud import CliCloudAdapter
    from puffo_agent.agent.harness import build_harness

    return CliCloudAdapter(
        agent_id="x",
        model="",
        workspace_dir=str(tmp_path),
        claude_dir=str(tmp_path),
        session_file=str(tmp_path / "s.json"),
        mcp_config_file=str(tmp_path / "m.json"),
        agent_home_dir=str(tmp_path / "home"),
        harness=build_harness(harness_name),
        **kw,
    )


def test_gateway_env_claude(tmp_path):
    a = _cloud_adapter(
        tmp_path, "claude-code",
        llm_gateway_url="https://gw", llm_api_key="vk",
    )
    env = a._llm_gateway_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://gw"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "vk"


def test_gateway_env_codex(tmp_path):
    a = _cloud_adapter(
        tmp_path, "codex",
        llm_gateway_url="https://gw", llm_api_key="vk",
    )
    env = a._llm_gateway_env()
    assert env["OPENAI_BASE_URL"] == "https://gw"
    assert env["OPENAI_API_KEY"] == "vk"


def test_gateway_env_empty_without_url(tmp_path):
    a = _cloud_adapter(tmp_path, "claude-code")
    assert a._llm_gateway_env() == {}


# ── Bridge stub + A1 inbound ──────────────────────────────────────────

def test_bridge_config_from_env(monkeypatch):
    monkeypatch.setenv("PUFFO_BRIDGE_URL", "https://b")
    monkeypatch.setenv("PUFFO_BRIDGE_TOKEN", "tok")
    monkeypatch.setenv("PUFFO_LLM_VIRTUAL_KEY", "vk")
    bc = BridgeConfig.from_env()
    assert bc.is_configured()
    assert bc.llm_virtual_key == "vk"


@pytest.mark.asyncio
async def test_inbound_dispatch_and_store():
    bridge = StubBridgeClient()
    store = _FakeStore()
    client = BridgeMessageClient(bridge=bridge, message_store=store, slug="me")
    got: list = []

    async def on_message(root_id, batch, meta):
        got.append((root_id, batch, meta))

    bridge.push_event(BridgeInboundEvent(
        root_id="r1",
        messages=[{
            "envelope_id": "e1", "content": "hi",
            "envelope_kind": "dm", "sender_slug": "alice",
        }],
        channel_meta={"k": "v"},
    ))
    task = asyncio.create_task(client.listen(on_message))
    await asyncio.sleep(0.1)
    await client.stop()
    await asyncio.wait_for(task, 2)

    assert got and got[0][0] == "r1"
    assert store.stored and store.stored[0]["envelope_id"] == "e1"
    assert client._last_dm_sender == "alice"


@pytest.mark.asyncio
async def test_reconnect_on_drop(monkeypatch):
    monkeypatch.setattr(mc_mod, "RECONNECT_BACKOFF_SECONDS", 0.01)
    bridge = StubBridgeClient()
    bridge.fail_next_run = True
    client = BridgeMessageClient(bridge=bridge, message_store=_FakeStore(), slug="me")
    got: list = []

    async def on_message(root_id, batch, meta):
        got.append(root_id)

    bridge.push_event(BridgeInboundEvent(root_id="r2", messages=[]))
    task = asyncio.create_task(client.listen(on_message))
    await asyncio.sleep(0.2)
    await client.stop()
    await asyncio.wait_for(task, 2)

    # Failed the first run, reconnected, then delivered.
    assert bridge.connects == 2
    assert got == ["r2"]


@pytest.mark.asyncio
async def test_send_fallback_uses_last_dm_sender():
    bridge = StubBridgeClient()
    client = BridgeMessageClient(bridge=bridge, message_store=_FakeStore(), slug="me")
    client._last_dm_sender = "bob"
    await client.send_fallback_message("", "reply text")
    assert bridge.sent[0]["channel"] == "@bob"
    assert bridge.sent[0]["text"] == "reply text"


# ── A2: MCP send_message → Bridge ─────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_tool_routes_to_bridge():
    from mcp.server.fastmcp import FastMCP

    from puffo_agent.mcp.puffo_core_tools import (
        PuffoCoreToolsConfig,
        register_core_tools,
    )

    bridge = StubBridgeClient()
    cfg = PuffoCoreToolsConfig(
        slug="me",
        device_id="d",
        keystore=None,
        http_client=None,
        data_client=None,
        bridge_outbound=bridge,
    )
    mcp = FastMCP("t")
    register_core_tools(mcp, cfg)
    await mcp.call_tool(
        "send_message",
        {"channel": "@alice", "text": "hello", "is_visible_to_human": True},
    )
    assert bridge.sent and bridge.sent[0]["text"] == "hello"
    assert bridge.sent[0]["channel"] == "@alice"
