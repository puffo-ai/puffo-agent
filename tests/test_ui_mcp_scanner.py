"""``_scan_mcp_servers`` reads the harness-specific config files only.

Codex agents must not see entries from ``.claude.json`` and vice versa
— mixing them would confuse the operator and surface MCPs the running
harness can't actually load.
"""
from __future__ import annotations

import json
import textwrap

import pytest


@pytest.fixture(autouse=True)
def _qt_offscreen(monkeypatch):
    # Importing the UI module pulls PySide6; force offscreen so the
    # test runner doesn't try to attach to a display.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


def _scan(*args, **kw):
    # Lazy import so the qt offscreen env var is applied first.
    from puffo_agent.portal.ui.widgets.agent_detail import _scan_mcp_servers
    return _scan_mcp_servers(*args, **kw)


def _make_claude_json(path, servers):
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _make_codex_toml(path, servers):
    body = ""
    for name, cfg in servers.items():
        body += f"\n[mcp_servers.{name}]\n"
        for k, v in cfg.items():
            if isinstance(v, list):
                body += f'{k} = {json.dumps(v)}\n'
            elif isinstance(v, dict):
                body += f"\n[mcp_servers.{name}.{k}]\n"
                for ek, ev in v.items():
                    body += f'{ek} = {json.dumps(ev)}\n'
            else:
                body += f'{k} = {json.dumps(v)}\n'
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_codex_harness_reads_only_codex_config(tmp_path):
    agent_root = tmp_path / "agent"
    home = tmp_path / "home"
    (agent_root / ".codex").mkdir(parents=True)
    _make_codex_toml(
        agent_root / ".codex" / "config.toml",
        {"puffo": {"command": "py", "args": ["-m", "puffo_agent.mcp"]}},
    )
    # Decoy: a claude-code config that codex MUST NOT pick up.
    _make_claude_json(
        agent_root / ".claude.json",
        {"unrelated": {"command": "should-not-appear"}},
    )

    entries = _scan(agent_root, home, "codex")
    assert [(s, n) for s, n, _ in entries] == [("agent", "puffo")]
    assert entries[0][2]["command"] == "py"


def test_claude_code_harness_reads_only_claude_configs(tmp_path):
    agent_root = tmp_path / "agent"
    home = tmp_path / "home"
    agent_root.mkdir()
    _make_claude_json(
        agent_root / ".claude.json",
        {"agent-server": {"command": "agent-py"}},
    )
    (home).mkdir()
    _make_claude_json(
        home / ".claude.json",
        {"host-server": {"command": "host-py"}},
    )
    (agent_root / "workspace").mkdir()
    _make_claude_json(
        agent_root / "workspace" / ".mcp.json",
        {"ws-server": {"command": "ws-py"}},
    )
    # Decoy codex entry — claude-code path must skip it.
    (agent_root / ".codex").mkdir()
    _make_codex_toml(
        agent_root / ".codex" / "config.toml",
        {"decoy": {"command": "nope"}},
    )

    entries = _scan(agent_root, home, "claude-code")
    names = sorted((s, n) for s, n, _ in entries)
    assert names == [
        ("agent", "agent-server"),
        ("agent workspace", "ws-server"),
        ("host", "host-server"),
    ]


def test_unknown_harness_returns_empty(tmp_path):
    agent_root = tmp_path / "agent"
    home = tmp_path / "home"
    agent_root.mkdir()
    home.mkdir()
    _make_claude_json(
        agent_root / ".claude.json",
        {"x": {"command": "x"}},
    )
    assert _scan(agent_root, home, "hermes") == []


def test_missing_config_files_are_skipped_silently(tmp_path):
    agent_root = tmp_path / "agent"
    home = tmp_path / "home"
    agent_root.mkdir()
    home.mkdir()
    assert _scan(agent_root, home, "codex") == []
    assert _scan(agent_root, home, "claude-code") == []


def test_malformed_config_does_not_break_scan(tmp_path):
    agent_root = tmp_path / "agent"
    home = tmp_path / "home"
    agent_root.mkdir()
    home.mkdir()
    (home).mkdir(exist_ok=True)
    (agent_root / ".claude.json").write_text("{not json", encoding="utf-8")
    _make_claude_json(
        home / ".claude.json",
        {"good-host": {"command": "good"}},
    )
    entries = _scan(agent_root, home, "claude-code")
    assert [(s, n) for s, n, _ in entries] == [("host", "good-host")]
