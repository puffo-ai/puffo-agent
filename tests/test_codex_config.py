"""Phase 4 tests — codex config.toml writer.

Tomllib is stdlib in Python 3.11+ so we round-trip what we wrote to
make sure the hand-rolled emitter actually produces valid TOML and
preserves every field codex looks at (command / args / env).
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.config import write_codex_mcp_config


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_round_trip_full_doc(tmp_path):
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={
            "PUFFO_CORE_SLUG": "alice-noun-abcd",
            "PUFFO_WORKSPACE": "/tmp/work",
            "PYTHONUSERBASE": "/Users/op/.local",
        },
    )
    doc = _read_toml(dest)
    servers = doc.get("mcp_servers") or {}
    assert "puffo" in servers
    puffo = servers["puffo"]
    assert puffo["command"] == "/usr/bin/python3"
    assert puffo["args"] == ["-m", "puffo_agent.mcp.puffo_core_server"]
    assert puffo["env"]["PUFFO_CORE_SLUG"] == "alice-noun-abcd"
    assert puffo["env"]["PUFFO_WORKSPACE"] == "/tmp/work"
    assert puffo["env"]["PYTHONUSERBASE"] == "/Users/op/.local"


def test_env_omitted_when_empty(tmp_path):
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/bin/echo",
        args=[],
        env={},
    )
    doc = _read_toml(dest)
    puffo = doc["mcp_servers"]["puffo"]
    # When env is empty we deliberately skip emitting the env table.
    assert "env" not in puffo


def test_paths_with_quotes_and_backslashes(tmp_path):
    """Windows paths + values containing quotes survive the round-trip
    without breaking TOML's basic-string escape rules."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command=r"C:\Users\op\AppData\python.exe",
        args=["-m", "x"],
        env={
            "WEIRD": 'a "quoted" value',
            "WIN_PATH": r"C:\Users\op",
        },
    )
    doc = _read_toml(dest)
    puffo = doc["mcp_servers"]["puffo"]
    assert puffo["command"] == r"C:\Users\op\AppData\python.exe"
    assert puffo["env"]["WEIRD"] == 'a "quoted" value'
    assert puffo["env"]["WIN_PATH"] == r"C:\Users\op"


# ── PUF-266: host MCP merge via extra_servers ────────────────────────────


def test_extra_servers_emitted_alongside_puffo(tmp_path):
    """Host has 2 MCP entries + puffo configured → emitted TOML has all
    three blocks. Existing host command/args/env preserved verbatim."""
    dest = tmp_path / "config.toml"
    extras = {
        "filesystem": {
            "command": "/usr/local/bin/mcp-fs",
            "args": ["--root", "/Users/op/projects"],
            "env": {"FS_LOG_LEVEL": "info"},
        },
        "github": {
            "command": "npx",
            "args": ["@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "ghp_redacted"},
        },
    }
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={"PUFFO_CORE_SLUG": "alice-noun-abcd"},
        extra_servers=extras,
    )
    doc = _read_toml(dest)
    servers = doc["mcp_servers"]
    assert set(servers) == {"puffo", "filesystem", "github"}
    assert servers["filesystem"]["command"] == "/usr/local/bin/mcp-fs"
    assert servers["filesystem"]["args"] == ["--root", "/Users/op/projects"]
    assert servers["filesystem"]["env"]["FS_LOG_LEVEL"] == "info"
    assert servers["github"]["command"] == "npx"
    assert servers["github"]["env"]["GITHUB_TOKEN"] == "ghp_redacted"
    # Puffo entry unaffected by the merge.
    assert servers["puffo"]["command"] == "/usr/bin/python3"
    assert servers["puffo"]["env"]["PUFFO_CORE_SLUG"] == "alice-noun-abcd"


def test_extra_servers_pass_through_when_puffo_unconfigured(tmp_path):
    """Operator running a codex agent without puffo_core configured
    still gets the host MCP catalog. Pre-PUF-266 behavior wrote only
    the auth-pin line; we now mirror host MCPs too."""
    dest = tmp_path / "config.toml"
    extras = {
        "filesystem": {
            "command": "/usr/local/bin/mcp-fs",
            "args": [], "env": {},
        },
    }
    write_codex_mcp_config(dest, extra_servers=extras)
    doc = _read_toml(dest)
    assert doc["cli_auth_credentials_store"] == "file"
    servers = doc.get("mcp_servers") or {}
    assert "filesystem" in servers
    assert "puffo" not in servers


def test_extra_servers_named_puffo_is_shadowed_by_puffo_entry(tmp_path):
    """If the host already has an entry named ``puffo`` (e.g., operator
    installed a third-party puffo MCP), the daemon's puffo entry wins.
    Otherwise we'd have two ``[mcp_servers.puffo]`` blocks which is
    invalid TOML AND the operator's third-party version could shadow
    our real one."""
    dest = tmp_path / "config.toml"
    extras = {
        "puffo": {
            "command": "/bin/imposter", "args": [],
            "env": {"NOT": "real"},
        },
        "filesystem": {
            "command": "/usr/local/bin/mcp-fs", "args": [], "env": {},
        },
    }
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={},
        extra_servers=extras,
    )
    doc = _read_toml(dest)
    # Only the daemon's puffo entry; imposter is dropped.
    assert doc["mcp_servers"]["puffo"]["command"] == "/usr/bin/python3"
    assert "NOT" not in doc["mcp_servers"]["puffo"].get("env", {})
    # Other host entries still merged.
    assert "filesystem" in doc["mcp_servers"]


def test_no_extras_unchanged_from_pre_puf266(tmp_path):
    """Without extra_servers (default) the emitted document matches
    the pre-PUF-266 shape exactly. Regression guard for callers that
    haven't been migrated to pass extra_servers yet."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={"X": "y"},
    )
    doc = _read_toml(dest)
    assert list(doc["mcp_servers"]) == ["puffo"]


# ── PR #54 review item 4b: TOML-key escape for non-bare-charset names ──


def test_extra_servers_with_dot_in_name_quotes_the_key(tmp_path):
    """A host entry named ``my.server`` MUST emit
    ``[mcp_servers."my.server"]`` (quoted basic-string key), not
    ``[mcp_servers.my.server]`` which TOML parses as nested tables."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        extra_servers={
            "my.server": {
                "command": "/bin/x", "args": [], "env": {},
            },
        },
    )
    doc = _read_toml(dest)
    servers = doc.get("mcp_servers") or {}
    assert "my.server" in servers, (
        "key with `.` got misparsed as nested tables — _toml_key "
        "didn't quote-escape"
    )
    assert "my" not in servers, (
        "TOML was emitted bare, creating accidental nested tables"
    )
    assert servers["my.server"]["command"] == "/bin/x"


def test_extra_servers_with_bare_name_left_unquoted(tmp_path):
    """Conversely, a bare-charset name (``[A-Za-z0-9_-]+``) must emit
    without surrounding quotes — cosmetic but matches operator-written
    config style."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        extra_servers={
            "filesystem-1": {"command": "/bin/x", "args": [], "env": {}},
        },
    )
    raw = dest.read_text(encoding="utf-8")
    assert "[mcp_servers.filesystem-1]" in raw
    assert '[mcp_servers."filesystem-1"]' not in raw
