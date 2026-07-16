"""Phase 4 tests — codex config.toml writer."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.config import (
    _toml_escape,
    _toml_key,
    write_codex_mcp_config,
)


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
    # Codex agent without puffo_core still inherits the host catalog.
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
    # Daemon's puffo entry wins over a host-side `[mcp_servers.puffo]`
    # — duplicate TOML keys would be invalid + host could shadow ours.
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
    # Default-args regression guard for callers not yet migrated.
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={"X": "y"},
    )
    doc = _read_toml(dest)
    assert list(doc["mcp_servers"]) == ["puffo"]


# ── _toml_key / _toml_escape unit-level coverage ──


def test_toml_key_bare_charset():
    # Pin the bare-key acceptance set.
    for name in ("filesystem", "fs-1", "fs_1", "FS", "abc123", "_x", "-x"):
        assert _toml_key(name) == name, f"bare-key rejected: {name!r}"


def test_toml_key_non_bare_quoted():
    # Anything outside [A-Za-z0-9_-] must be wrapped + escape-applied.
    assert _toml_key("my.server") == '"my.server"'
    assert _toml_key("name with spaces") == '"name with spaces"'
    assert _toml_key('quoted"name') == '"quoted\\"name"'
    assert _toml_key("back\\slash") == '"back\\\\slash"'


def test_toml_escape_metacharacters():
    # Only backslash + double-quote are escaped; others passthrough.
    assert _toml_escape("plain") == "plain"
    assert _toml_escape('a"b') == 'a\\"b'
    assert _toml_escape("a\\b") == "a\\\\b"
    assert _toml_escape("\\\"") == '\\\\\\"'  # backslash then quote
    # Unicode + non-ASCII passes through unmodified.
    assert _toml_escape("café") == "café"


def test_extra_servers_with_dot_in_name_quotes_the_key(tmp_path):
    # `my.server` must NOT parse as nested tables (TOML would otherwise
    # create `mcp_servers.my.server` accidentally).
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
    assert "my.server" in servers
    assert "my" not in servers
    assert servers["my.server"]["command"] == "/bin/x"


def test_extra_servers_with_bare_name_left_unquoted(tmp_path):
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


def test_env_keys_with_dots_are_quoted(tmp_path):
    # Symmetry: env keys go through _toml_key too, so weird names like
    # `MY.VAR` don't become nested tables inside the env subtable.
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        extra_servers={
            "fs": {
                "command": "/bin/x",
                "args": [],
                "env": {"MY.VAR": "value"},
            },
        },
    )
    doc = _read_toml(dest)
    assert doc["mcp_servers"]["fs"]["env"]["MY.VAR"] == "value"


def test_litellm_provider_block(tmp_path):
    """Gateway/VK path: a provider dict emits a codex custom model_provider
    pointing at the LiteLLM gateway with wire_api=responses."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        provider={
            "name": "litellm",
            "base_url": "https://gw.example/v1",
            "env_key": "OPENAI_API_KEY",
            "model": "codex",
            "wire_api": "responses",
        },
    )
    doc = _read_toml(dest)
    assert doc["model"] == "codex"
    assert doc["model_provider"] == "litellm"
    prov = doc["model_providers"]["litellm"]
    assert prov["base_url"] == "https://gw.example/v1"
    assert prov["env_key"] == "OPENAI_API_KEY"
    assert prov["wire_api"] == "responses"
    # cli_auth store still emitted (unchanged) — codex file-mode auth.
    assert doc["cli_auth_credentials_store"] == "file"


def test_no_provider_block_when_absent(tmp_path):
    """OAuth path (no provider) must not emit any model_provider — preserves
    the existing ChatGPT-OAuth behavior."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(dest)
    raw = dest.read_text(encoding="utf-8")
    assert "model_provider" not in raw
    assert "[model_providers" not in raw
    doc = _read_toml(dest)
    assert doc["cli_auth_credentials_store"] == "file"


def test_provider_coexists_with_puffo_mcp(tmp_path):
    """Provider block + puffo_core MCP server in one doc still parses (bare
    keys before all tables)."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={"PUFFO_CORE_SLUG": "bob-verb-1234"},
        provider={
            "name": "litellm",
            "base_url": "https://gw.example/v1",
            "env_key": "OPENAI_API_KEY",
            "model": "gpt-5.2-codex",
        },
    )
    doc = _read_toml(dest)
    assert doc["model_provider"] == "litellm"
    assert doc["model_providers"]["litellm"]["wire_api"] == "responses"  # default
    assert doc["mcp_servers"]["puffo"]["env"]["PUFFO_CORE_SLUG"] == "bob-verb-1234"
