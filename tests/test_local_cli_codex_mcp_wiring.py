"""PUF-266 wiring integration: _ensure_codex_session reads host MCPs
and forwards them to write_codex_mcp_config. Unit tests cover each side
in isolation; this defends the wiring between them."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
from puffo_agent.agent.harness import CodexHarness


def _make_adapter(
    tmp_path: Path,
    *,
    puffo_core_env: dict | None = None,
) -> LocalCLIAdapter:
    agent_id = "agent-puf266-wiring"
    adapter = LocalCLIAdapter(
        agent_id=agent_id,
        model="",
        workspace_dir=str(tmp_path / "workspace"),
        claude_dir=str(tmp_path / "claude"),
        session_file=str(tmp_path / "session.json"),
        mcp_config_file=str(tmp_path / "mcp_config.json"),
        agent_home_dir=str(tmp_path / "agents" / agent_id),
        harness=CodexHarness(),
        permission_mode="bypassPermissions",
    )
    adapter.puffo_core_mcp_env = puffo_core_env
    return adapter


def _seed_host_codex_config(host_home: Path, body: str) -> Path:
    cfg = host_home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_ensure_codex_session_merges_host_mcps_into_config_toml(
    tmp_path, monkeypatch,
):
    # Pins wiring: future refactor dropping extra_servers=host_mcps
    # would silently regress operator-host-MCP merge.
    host_home = tmp_path / "host"
    host_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host_home))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))

    _seed_host_codex_config(
        host_home,
        '[mcp_servers.filesystem]\n'
        'command = "/usr/local/bin/mcp-fs"\n'
        'args = ["--root", "/Users/op"]\n'
        '\n'
        '[mcp_servers.filesystem.env]\n'
        'FS_LOG_LEVEL = "info"\n',
    )

    # _ensure_codex_session writes config.toml THEN tries to spawn
    # codex; spawn fails in test env but the file is already on disk.
    adapter = _make_adapter(
        tmp_path,
        puffo_core_env={
            "PUFFO_CORE_SLUG": "alice", "PUFFO_WORKSPACE": str(tmp_path),
        },
    )
    with pytest.raises(RuntimeError):
        adapter._ensure_codex_session()

    codex_home = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / adapter.agent_id / ".codex"
    config_toml = codex_home / "config.toml"
    assert config_toml.exists(), "config.toml not written by _ensure_codex_session"

    doc = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    servers = doc.get("mcp_servers") or {}
    assert "filesystem" in servers
    assert servers["filesystem"]["command"] == "/usr/local/bin/mcp-fs"
    assert servers["filesystem"]["args"] == ["--root", "/Users/op"]
    assert servers["filesystem"]["env"]["FS_LOG_LEVEL"] == "info"
    assert "puffo" in servers
    assert servers["puffo"]["env"]["PUFFO_CORE_SLUG"] == "alice"


def test_ensure_codex_session_honors_CODEX_HOME_env_for_host_read(
    tmp_path, monkeypatch,
):
    host_home = tmp_path / "host"
    custom_codex = tmp_path / "custom-codex"
    host_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host_home))
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))

    # Seed CUSTOM location, NOT default ~/.codex.
    custom_codex.mkdir()
    (custom_codex / "config.toml").write_text(
        '[mcp_servers.fs_custom]\n'
        'command = "/usr/local/bin/mcp-fs-custom"\n'
        'args = []\n',
        encoding="utf-8",
    )
    # Also seed default with a marker so hardcoded-path regression is visible.
    (host_home / ".codex").mkdir()
    (host_home / ".codex" / "config.toml").write_text(
        '[mcp_servers.fs_default_should_be_ignored]\n'
        'command = "/usr/local/bin/mcp-fs-default"\n'
        'args = []\n',
        encoding="utf-8",
    )

    adapter = _make_adapter(tmp_path, puffo_core_env=None)
    with pytest.raises(RuntimeError):
        adapter._ensure_codex_session()

    codex_home = Path(os.environ["PUFFO_AGENT_HOME"]) / "agents" / adapter.agent_id / ".codex"
    doc = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    servers = doc.get("mcp_servers") or {}
    assert "fs_custom" in servers
    assert "fs_default_should_be_ignored" not in servers
