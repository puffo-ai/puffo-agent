"""Tests for ``portal.host_mcp_handler.install`` / ``sync`` — the
daemon-side handlers the rpc_service dispatches into."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from puffo_agent.portal import host_mcp_handler
from puffo_agent.portal.host_mcp_handler import HostMcpContext


def _ctx(
    tmp_path: Path,
    *,
    http_get=None,
    http_post=None,
    harness: str = "claude-code",
) -> HostMcpContext:
    """Minimal context wired with mocks for the two HTTP surfaces
    install / sync touch (catalog GET + DM POST). ``harness`` selects
    which host file the install lands in."""
    host_home = tmp_path / "operator-home"
    host_home.mkdir()
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    http = MagicMock()
    http.get = AsyncMock(side_effect=http_get or AsyncMock())
    http.post = AsyncMock(side_effect=http_post or AsyncMock(
        return_value={"envelope_id": "env_test_1"},
    ))
    keystore = MagicMock()
    # Just enough surface for _send_dm_to_operator to not blow up at
    # the type-coercion step. The fetch_device_keys call goes through
    # http.get above, so we control whether DM send succeeds or fails
    # by what we make .get / .post do.
    sess = MagicMock()
    sess.subkey_id = "subkey_test"
    sess.subkey_secret_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    keystore.load_session = MagicMock(return_value=sess)
    return HostMcpContext(
        agent_id="agent_test",
        slug="bot-test",
        operator_slug="op-test",
        host_home=host_home,
        agent_home=agent_home,
        harness=harness,
        keystore=keystore,
        http_client=http,
    )


def _write_host_claude_json(host_home: Path, servers: dict[str, Any]) -> None:
    (host_home / ".claude.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8",
    )


# ── install ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_rejects_both_template_id_and_spec(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="exactly one of"):
        await host_mcp_handler.install(
            ctx, name="x", template_id="y", spec={"type": "stdio", "command": "x"},
        )


@pytest.mark.asyncio
async def test_install_rejects_neither_template_id_nor_spec(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="exactly one of"):
        await host_mcp_handler.install(ctx, name="x")


@pytest.mark.asyncio
async def test_install_rejects_missing_operator_slug(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.operator_slug = ""
    with pytest.raises(RuntimeError, match="no operator_slug bound"):
        await host_mcp_handler.install(
            ctx, name="x", spec={"type": "stdio", "command": "x"},
        )


@pytest.mark.asyncio
async def test_install_already_present_skips_write_and_dm(tmp_path):
    ctx = _ctx(tmp_path)
    _write_host_claude_json(
        ctx.host_home,
        {"gmail-read": {"type": "stdio", "command": "node"}},
    )
    msg = await host_mcp_handler.install(
        ctx, name="gmail-read",
        spec={"type": "stdio", "command": "node"},
    )
    assert "already registered" in msg
    # No POST was sent (DM is the only post path for this handler).
    assert ctx.http_client.post.call_count == 0


@pytest.mark.asyncio
async def test_install_adhoc_spec_writes_host_and_dms(tmp_path, monkeypatch):
    """Happy path: adhoc spec → host write → DM send → returns the
    success body referencing the envelope_id. DM-send is patched at
    the function boundary (crypto innards aren't this test's
    concern — that lives in encrypt_message's own tests)."""
    ctx = _ctx(tmp_path)
    dm_calls: list[tuple[Any, str]] = []
    async def _stub_dm(ctx_, text):
        dm_calls.append((ctx_, text))
        return "env_install_ok"
    monkeypatch.setattr(host_mcp_handler, "_send_dm_to_operator", _stub_dm)

    msg = await host_mcp_handler.install(
        ctx, name="coinbase-cdp-docs",
        spec={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@coinbase/cdp-docs-mcp"],
            "env": {},
        },
    )

    written = json.loads(
        (ctx.host_home / ".claude.json").read_text(encoding="utf-8"),
    )
    assert "coinbase-cdp-docs" in written["mcpServers"]
    assert written["mcpServers"]["coinbase-cdp-docs"]["command"] == "npx"
    # DM body got the bolded display_name (which falls back to
    # ``name`` on the adhoc path since there's no catalog row).
    assert len(dm_calls) == 1
    assert "**coinbase-cdp-docs**" in dm_calls[0][1]
    # Success body references the stub-returned envelope id.
    assert "env_install_ok" in msg
    assert "op-test" in msg


@pytest.mark.asyncio
async def test_install_dm_failure_returns_retry_body(tmp_path, monkeypatch):
    """Host write succeeded but DM send raised → return body so the
    agent can retry via send_message."""
    ctx = _ctx(tmp_path)
    async def _stub_dm(ctx_, text):
        raise RuntimeError("no recipient devices resolved")
    monkeypatch.setattr(host_mcp_handler, "_send_dm_to_operator", _stub_dm)

    msg = await host_mcp_handler.install(
        ctx, name="x",
        spec={"type": "stdio", "command": "node", "args": [], "env": {}},
    )

    # Host write still landed.
    written = json.loads(
        (ctx.host_home / ".claude.json").read_text(encoding="utf-8"),
    )
    assert "x" in written["mcpServers"]
    # Body reads as a retry hint, with the original DM text quoted.
    assert "DM" in msg and "send_message" in msg
    assert "no recipient devices resolved" in msg


@pytest.mark.asyncio
async def test_install_validation_error_does_not_write_file(tmp_path):
    """Spec with missing command for stdio transport → RuntimeError
    before any file write."""
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="command is required"):
        await host_mcp_handler.install(
            ctx, name="bad",
            spec={"type": "stdio", "args": []},
        )
    assert not (ctx.host_home / ".claude.json").exists()


# ── sync ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_host_entry_returns_hint(tmp_path):
    ctx = _ctx(tmp_path)
    msg = await host_mcp_handler.sync(ctx, template_id="gmail-read")
    assert "no entry" in msg
    assert "install_host_mcp" in msg


@pytest.mark.asyncio
async def test_sync_copies_host_entry_to_agent(tmp_path):
    ctx = _ctx(tmp_path)
    entry = {"type": "stdio", "command": "node", "args": ["server.js"], "env": {"TOKEN": "secret"}}
    _write_host_claude_json(ctx.host_home, {"gmail-read": entry})

    msg = await host_mcp_handler.sync(ctx, template_id="gmail-read")

    agent_data = json.loads(
        (ctx.agent_home / ".claude.json").read_text(encoding="utf-8"),
    )
    assert agent_data["mcpServers"]["gmail-read"] == entry
    assert "refresh()" in msg


# ── codex harness path ─────────────────────────────────────────────


def _read_host_codex(host_home: Path) -> str:
    return (host_home / ".codex" / "config.toml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_install_unsupported_harness_rejects_upfront(tmp_path):
    """hermes / gemini-cli don't have a known MCP config file we can
    write to. Fail fast rather than land in the wrong file."""
    ctx = _ctx(tmp_path, harness="hermes")
    with pytest.raises(RuntimeError, match="harness 'hermes' is not supported"):
        await host_mcp_handler.install(
            ctx, name="x",
            spec={"type": "stdio", "command": "node", "args": [], "env": {}},
        )


@pytest.mark.asyncio
async def test_install_codex_rejects_http_transport(tmp_path):
    ctx = _ctx(tmp_path, harness="codex")
    with pytest.raises(RuntimeError, match="codex agents only support stdio"):
        await host_mcp_handler.install(
            ctx, name="x",
            spec={"type": "http", "url": "https://x.example/mcp"},
        )


@pytest.mark.asyncio
async def test_install_codex_appends_toml_block(tmp_path, monkeypatch):
    """Happy path on codex: adhoc spec → toml block appended to
    operator's ~/.codex/config.toml → operator DM'd."""
    ctx = _ctx(tmp_path, harness="codex")
    monkeypatch.setattr(
        host_mcp_handler, "_send_dm_to_operator",
        AsyncMock(return_value="env_codex_ok"),
    )
    # Pre-existing host config with operator's own scalar key — must
    # round-trip intact (we append, not regenerate).
    host_codex = ctx.host_home / ".codex" / "config.toml"
    host_codex.parent.mkdir(parents=True, exist_ok=True)
    host_codex.write_text(
        'model_provider = "openai"\n', encoding="utf-8",
    )

    msg = await host_mcp_handler.install(
        ctx, name="coinbase-cdp-docs",
        spec={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@coinbase/cdp-docs-mcp"],
            "env": {},
        },
    )

    out = _read_host_codex(ctx.host_home)
    # Operator's pre-existing scalar still there.
    assert 'model_provider = "openai"' in out
    # New block appended.
    assert "[mcp_servers.coinbase-cdp-docs]" in out
    assert 'command = "npx"' in out
    assert "env_codex_ok" in msg
    assert "~/.codex/config.toml" in msg


@pytest.mark.asyncio
async def test_install_codex_already_present_short_circuits(tmp_path):
    ctx = _ctx(tmp_path, harness="codex")
    host_codex = ctx.host_home / ".codex" / "config.toml"
    host_codex.parent.mkdir(parents=True, exist_ok=True)
    host_codex.write_text(
        '[mcp_servers.gmail-read]\ncommand = "node"\nargs = []\n',
        encoding="utf-8",
    )
    before = host_codex.read_text(encoding="utf-8")

    msg = await host_mcp_handler.install(
        ctx, name="gmail-read",
        spec={"type": "stdio", "command": "node", "args": [], "env": {}},
    )

    assert "already registered" in msg
    # File untouched.
    assert host_codex.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_sync_codex_validates_host_entry_present(tmp_path):
    ctx = _ctx(tmp_path, harness="codex")
    msg = await host_mcp_handler.sync(ctx, template_id="gmail-read")
    assert "no entry" in msg
    assert "install_host_mcp" in msg


@pytest.mark.asyncio
async def test_sync_codex_does_not_write_agent_file(tmp_path):
    """Codex sync verifies host has the entry and points the agent
    at refresh() — the worker's restart code does the re-merge.
    Agent-side write would just get overwritten with the same
    content on the next restart, so we skip it."""
    ctx = _ctx(tmp_path, harness="codex")
    host_codex = ctx.host_home / ".codex" / "config.toml"
    host_codex.parent.mkdir(parents=True, exist_ok=True)
    host_codex.write_text(
        '[mcp_servers.gmail-read]\ncommand = "node"\nargs = []\n',
        encoding="utf-8",
    )

    msg = await host_mcp_handler.sync(ctx, template_id="gmail-read")

    assert "refresh()" in msg
    assert "re-merges" in msg
    # No file under agent_home/.codex was touched.
    assert not (ctx.agent_home / ".codex").exists()
