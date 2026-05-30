"""Tests for ``link_host_codex_auth`` — the OAuth fallback path that
shares ``~/.codex/auth.json`` with each agent's ``$CODEX_HOME``.

Mirrors ``test_host_credentials.py``'s shape: symlink-preferred,
copy-fallback for Windows non-dev-mode, idempotent on second call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import link_host_codex_auth


def _symlinks_available(tmp_path: Path) -> bool:
    probe = tmp_path / "_probe_link"
    target = tmp_path / "_probe_target"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, probe)
        probe.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        try:
            target.unlink()
        except OSError:
            pass
        return False


def _write_host_auth(host_home: Path, content: str = '{"token": "v1"}') -> Path:
    p = host_home / ".codex" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_no_host_file_returns_marker(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "no-host-file"
    assert not agent_codex.exists()


def test_symlink_created_when_supported(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    host_auth = _write_host_auth(host, '{"token": "v1"}')

    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "symlink"
    agent_auth = agent_codex / "auth.json"
    assert agent_auth.is_symlink()
    assert agent_auth.read_text() == '{"token": "v1"}'
    # Host rewrite (OAuth refresh) is visible through the symlink with
    # no relink — the property the symlink path exists for.
    host_auth.write_text('{"token": "v2"}', encoding="utf-8")
    assert agent_auth.read_text() == '{"token": "v2"}'


def test_symlink_idempotent_on_second_call(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host)
    link_host_codex_auth(host, agent_codex)
    # Second call must not re-create — returns the (already) marker so
    # the daemon log doesn't spam "shared host codex auth (symlink)"
    # every tick.
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "symlink (already)"


def test_copy_path_when_symlink_fails(tmp_path, monkeypatch):
    # Force the os.symlink call inside state.py to raise so we exercise
    # the copy fallback regardless of platform.
    import puffo_agent.portal.state as state_mod
    monkeypatch.setattr(
        state_mod.os, "symlink",
        lambda *a, **k: (_ for _ in ()).throw(NotImplementedError("no symlink")),
    )
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host, '{"token": "v1"}')

    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "copy"
    agent_auth = agent_codex / "auth.json"
    assert agent_auth.exists()
    assert not agent_auth.is_symlink()
    assert agent_auth.read_text() == '{"token": "v1"}'


def test_copy_fresh_marker_when_already_in_sync(tmp_path, monkeypatch):
    """Copy-mode second call sees the destination is up-to-date and
    returns ``copy (fresh)``."""
    import puffo_agent.portal.state as state_mod
    monkeypatch.setattr(
        state_mod.os, "symlink",
        lambda *a, **k: (_ for _ in ()).throw(NotImplementedError("no symlink")),
    )
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host)
    link_host_codex_auth(host, agent_codex)
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "copy (fresh)"


# ── PUF-266: read_host_codex_mcp_servers ────────────────────────────


from puffo_agent.portal.state import read_host_codex_mcp_servers


def test_read_host_codex_mcp_servers_returns_empty_when_no_host_config(tmp_path):
    assert read_host_codex_mcp_servers(tmp_path) == {}


def test_read_host_codex_mcp_servers_returns_empty_when_malformed(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("this is = not [valid toml\n", encoding="utf-8")
    assert read_host_codex_mcp_servers(tmp_path) == {}


def test_read_host_codex_mcp_servers_parses_basic_entries(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        'cli_auth_credentials_store = "file"\n'
        '\n'
        '[mcp_servers.filesystem]\n'
        'command = "/usr/local/bin/mcp-fs"\n'
        'args = ["--root", "/Users/op"]\n'
        '\n'
        '[mcp_servers.filesystem.env]\n'
        'FS_LOG_LEVEL = "info"\n'
        '\n'
        '[mcp_servers.github]\n'
        'command = "npx"\n'
        'args = ["@modelcontextprotocol/server-github"]\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert set(out) == {"filesystem", "github"}
    assert out["filesystem"]["command"] == "/usr/local/bin/mcp-fs"
    assert out["filesystem"]["args"] == ["--root", "/Users/op"]
    assert out["filesystem"]["env"]["FS_LOG_LEVEL"] == "info"
    assert out["github"]["args"] == ["@modelcontextprotocol/server-github"]
    # Missing env defaults to {} (not absent / not None).
    assert out["github"]["env"] == {}


def test_read_host_codex_mcp_servers_ignores_top_level_non_mcp(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        'cli_auth_credentials_store = "file"\n'
        'model = "gpt-5-codex"\n'
        '[mcp_servers.fs]\ncommand = "x"\nargs = []\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert list(out) == ["fs"]


def test_read_host_codex_mcp_servers_skips_entries_with_no_command(tmp_path):
    # PR #54 review item 4a: empty/missing command would land as
    # `command = ""` in per-agent TOML and crash codex on empty argv.
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers.missing_cmd]\n'
        'args = ["x"]\n'
        '\n'
        '[mcp_servers.empty_cmd]\n'
        'command = ""\n'
        'args = []\n'
        '\n'
        '[mcp_servers.non_string_cmd]\n'
        'command = 42\n'
        'args = []\n'
        '\n'
        '[mcp_servers.ok_entry]\n'
        'command = "/bin/x"\n'
        'args = []\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert set(out) == {"ok_entry"}


def test_read_host_codex_mcp_servers_honors_CODEX_HOME_env(tmp_path, monkeypatch):
    custom_codex = tmp_path / "custom-codex-home"
    custom_codex.mkdir()
    (custom_codex / "config.toml").write_text(
        '[mcp_servers.fs]\ncommand = "/bin/fs"\nargs = []\n',
        encoding="utf-8",
    )
    # Also seed default location so a hardcoded-path regression surfaces.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.fs_default]\ncommand = "/bin/no"\nargs = []\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    out = read_host_codex_mcp_servers(tmp_path)
    assert set(out) == {"fs"}


def test_read_host_codex_mcp_servers_defaults_when_CODEX_HOME_unset(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.fs]\ncommand = "/bin/fs"\nargs = []\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert set(out) == {"fs"}


def test_read_host_codex_mcp_servers_drops_non_dict_spec(tmp_path):
    # A non-table mcp_servers entry (e.g. accidentally a string) must
    # be skipped, not raise. Pins the `isinstance(spec, dict)` guard.
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers]\n'
        'broken_entry = "not_a_table"\n'
        '\n'
        '[mcp_servers.ok]\n'
        'command = "/bin/x"\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert set(out) == {"ok"}


def test_read_host_codex_mcp_servers_coerces_non_list_args_to_empty(tmp_path):
    # `args = "string"` is parseable TOML but semantically wrong. Without
    # the isinstance(raw_args, list) guard, `list(str)` would split into
    # chars and the per-agent config would emit a nonsense argv.
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers.weird]\n'
        'command = "/bin/x"\n'
        'args = "not_a_list"\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert out["weird"]["args"] == []


def test_read_host_codex_mcp_servers_coerces_non_dict_env_to_empty(tmp_path):
    # `env = "string"` would crash dict(str) without the
    # isinstance(raw_env, dict) guard.
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[mcp_servers.weird]\n'
        'command = "/bin/x"\n'
        'args = []\n'
        'env = "not_a_table"\n',
        encoding="utf-8",
    )
    out = read_host_codex_mcp_servers(tmp_path)
    assert out["weird"]["env"] == {}
