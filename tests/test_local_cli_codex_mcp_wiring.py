"""PUF-266 PR #54 review item 1: integration test that pins
``_ensure_codex_session`` reads host MCPs via
``read_host_codex_mcp_servers`` and forwards them through
``write_codex_mcp_config(extra_servers=...)``. The unit-level isolation
tests (``test_codex_config.py`` + ``test_codex_auth_link.py``) cover
each side; this one defends against a future refactor that drops the
``extra_servers=host_mcps`` parameter without breaking either side."""

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
    """Build a cli-local adapter configured for codex without spawning
    the subprocess. Caller takes the adapter through
    ``_ensure_codex_session`` and inspects the config.toml that
    ``write_codex_mcp_config`` lands on disk."""
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
    """``_ensure_codex_session`` must read the host's
    ``~/.codex/config.toml`` MCP servers and pass them through to the
    per-agent ``config.toml`` write. Wiring guard: future refactor that
    drops the ``extra_servers=host_mcps`` parameter would silently
    regress the operator-host-MCP merge — this test breaks loudly."""
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

    # PUF-266 wired through agent_id "agent-puf266-wiring"; the helper
    # writes config.toml then tries to spawn codex. The spawn fails
    # (no codex binary in test env / no host auth.json) BEFORE the
    # session is returned — but the config.toml IS already on disk
    # by then, which is what we're verifying.
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
    assert "filesystem" in servers, (
        "host MCP entry didn't make it into the per-agent config.toml — "
        "_ensure_codex_session may have dropped extra_servers=host_mcps"
    )
    assert servers["filesystem"]["command"] == "/usr/local/bin/mcp-fs"
    assert servers["filesystem"]["args"] == ["--root", "/Users/op"]
    assert servers["filesystem"]["env"]["FS_LOG_LEVEL"] == "info"
    # Puffo entry lands alongside.
    assert "puffo" in servers
    assert servers["puffo"]["env"]["PUFFO_CORE_SLUG"] == "alice"


def test_ensure_codex_session_honors_CODEX_HOME_env_for_host_read(
    tmp_path, monkeypatch,
):
    """PR #54 review item 2: operator-set ``$CODEX_HOME`` must be read
    instead of ``~/.codex``. Without this, an operator who keeps their
    codex config at a non-default location silently gets an empty host
    MCP catalog merged into agents."""
    host_home = tmp_path / "host"
    custom_codex = tmp_path / "custom-codex"
    host_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: host_home))
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))

    # Seed the CUSTOM location, NOT the default ~/.codex.
    custom_codex.mkdir()
    (custom_codex / "config.toml").write_text(
        '[mcp_servers.fs_custom]\n'
        'command = "/usr/local/bin/mcp-fs-custom"\n'
        'args = []\n',
        encoding="utf-8",
    )
    # Also seed the DEFAULT location with a marker MCP so a regression
    # back to the hardcoded path shows up clearly in the assertion.
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
    assert "fs_custom" in servers, "$CODEX_HOME override not honoured"
    assert "fs_default_should_be_ignored" not in servers, (
        "default ~/.codex was read even though $CODEX_HOME was set"
    )
