"""Codex snapshots MCP at session start; when the puffo tool surface
changes, cli-local codex agents must drop their session on daemon boot
so they reload the tools (openai/codex#7767)."""
import pytest

from puffo_agent.mcp.puffo_core_server import mcp_tool_fingerprint
from puffo_agent.portal.daemon import (
    _mcp_fingerprint_path,
    _respawn_codex_on_mcp_change_at_startup,
)
from puffo_agent.portal.state import (
    AgentConfig,
    RuntimeConfig,
    refresh_session_flag_path,
)


def _agent(aid: str, *, kind: str, harness: str) -> AgentConfig:
    cfg = AgentConfig(
        id=aid, display_name=aid,
        runtime=RuntimeConfig(kind=kind, harness=harness, model="m"),
    )
    cfg.save()
    return cfg


def _has_session_flag(cfg: AgentConfig) -> bool:
    return refresh_session_flag_path(cfg.resolve_workspace_dir()).exists()


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))


def test_fingerprint_is_stable_and_hex():
    a = mcp_tool_fingerprint()
    assert a == mcp_tool_fingerprint()
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_first_run_records_fingerprint_no_respawn(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    c = _agent("codex-1", kind="cli-local", harness="codex")
    assert not _mcp_fingerprint_path().exists()
    _respawn_codex_on_mcp_change_at_startup()
    assert _mcp_fingerprint_path().read_text().strip() == mcp_tool_fingerprint()
    assert not _has_session_flag(c)


def test_unchanged_fingerprint_no_respawn(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    c = _agent("codex-1", kind="cli-local", harness="codex")
    _mcp_fingerprint_path().write_text(mcp_tool_fingerprint() + "\n", encoding="utf-8")
    _respawn_codex_on_mcp_change_at_startup()
    assert not _has_session_flag(c)


def test_changed_fingerprint_respawns_only_cli_local_codex(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    codex_local = _agent("codex-local", kind="cli-local", harness="codex")
    codex_docker = _agent("codex-docker", kind="cli-docker", harness="codex")
    claude_local = _agent("claude-local", kind="cli-local", harness="claude-code")
    ws_agent = _agent("ws-agent", kind="ws-local", harness="codex")
    _mcp_fingerprint_path().write_text("STALE\n", encoding="utf-8")

    _respawn_codex_on_mcp_change_at_startup()

    assert _has_session_flag(codex_local)
    assert not _has_session_flag(codex_docker)
    assert not _has_session_flag(claude_local)
    assert not _has_session_flag(ws_agent)
    assert _mcp_fingerprint_path().read_text().strip() == mcp_tool_fingerprint()
