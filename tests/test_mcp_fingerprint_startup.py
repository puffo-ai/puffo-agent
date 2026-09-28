"""Startup MCP fingerprint: record + log only, sessions preserved."""
import logging

from puffo_agent.mcp.config import MONID_TOOL_NAMES
from puffo_agent.mcp.puffo_core_server import (
    _capture_tool_surface,
    _captured_tool_schema,
    mcp_tool_fingerprint,
)
from puffo_agent.portal.daemon import (
    _mcp_fingerprint_path,
    _record_mcp_fingerprint_at_startup,
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


def test_monid_gate_moves_fingerprint_and_toggles_exactly_monid(monkeypatch):
    monkeypatch.setenv("PUFFO_MONID_TOOLS_ENABLED", "false")
    off_fp, off_surface = mcp_tool_fingerprint(), set(_capture_tool_surface())
    monkeypatch.setenv("PUFFO_MONID_TOOLS_ENABLED", "true")
    on_fp, on_surface = mcp_tool_fingerprint(), set(_capture_tool_surface())

    assert on_fp != off_fp
    assert on_surface - off_surface == set(MONID_TOOL_NAMES)


def test_fingerprint_surface_covers_memory_family(monkeypatch):
    monkeypatch.setenv("PUFFO_MONID_TOOLS_ENABLED", "true")
    surface = set(_capture_tool_surface())
    assert {"create_note", "read_memory_file", "search_memory"} <= surface


def test_captured_tool_schema_is_address_free_and_captures_doc():
    # defaults excluded: sentinel would leak a process address
    sentinel = object()

    def sample(a: str, b: int = 5, c=sentinel):
        """sample doc"""

    schema = _captured_tool_schema(sample)
    by_name = {p["name"]: p for p in schema["params"]}

    assert schema["doc"] == "sample doc"
    assert by_name["a"]["required"] is True
    assert by_name["b"]["required"] is False and by_name["c"]["required"] is False
    import json

    assert "0x" not in json.dumps(schema)


def test_first_run_records_fingerprint(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    c = _agent("codex-1", kind="cli-local", harness="codex")
    assert not _mcp_fingerprint_path().exists()
    _record_mcp_fingerprint_at_startup()
    assert _mcp_fingerprint_path().read_text().strip() == mcp_tool_fingerprint()
    assert not _has_session_flag(c)


def test_unchanged_fingerprint_is_quiet(tmp_path, monkeypatch, caplog):
    _home(tmp_path, monkeypatch)
    c = _agent("codex-1", kind="cli-local", harness="codex")
    _mcp_fingerprint_path().write_text(mcp_tool_fingerprint() + "\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="puffo_agent.portal.daemon"):
        _record_mcp_fingerprint_at_startup()
    assert not _has_session_flag(c)
    assert "mcp tool surface changed" not in caplog.text


def test_changed_fingerprint_preserves_every_session(tmp_path, monkeypatch, caplog):
    """Fingerprint change: log only, no session flags."""
    _home(tmp_path, monkeypatch)
    agents = [
        _agent("codex-local", kind="cli-local", harness="codex"),
        _agent("codex-docker", kind="cli-docker", harness="codex"),
        _agent("claude-local", kind="cli-local", harness="claude-code"),
    ]
    _mcp_fingerprint_path().write_text("STALE\n", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="puffo_agent.portal.daemon"):
        _record_mcp_fingerprint_at_startup()

    assert not any(_has_session_flag(cfg) for cfg in agents)
    assert _mcp_fingerprint_path().read_text().strip() == mcp_tool_fingerprint()
    assert "native sessions preserved" in caplog.text


def test_record_survives_fingerprint_failure(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from puffo_agent.mcp import puffo_core_server as s

    def _boom():
        raise RuntimeError("fingerprint blew up")

    monkeypatch.setattr(s, "mcp_tool_fingerprint", _boom)
    _record_mcp_fingerprint_at_startup()
    assert not _mcp_fingerprint_path().exists()


def test_unreadable_fingerprint_file_is_rewritten(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _mcp_fingerprint_path().mkdir()
    _record_mcp_fingerprint_at_startup()
    assert _mcp_fingerprint_path().is_dir()
