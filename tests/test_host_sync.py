"""Tests for one-way sync of host-installed skills and MCP server
registrations into a cli-docker agent's per-agent virtual $HOME.

Contract:
  * Skills: copy each host-side ``<name>/SKILL.md`` dir wholesale
    into ``<agent_home>/.claude/skills/``, drop a ``host-synced.md``
    marker for provenance, prune stale host-synced dirs the host
    removed, never clobber a dir tagged ``agent-installed.md``.
  * MCPs: merge host ``~/.claude.json`` ``mcpServers`` into the
    per-agent ``.claude.json``; host wins on collision; agent-only
    entries survive; other top-level keys are left untouched.
  * Unreachable detection: absolute paths under ``/Users/``,
    ``/home/<someone>/``, ``/tmp/``, ``/var/folders/`` or any
    Windows drive-letter/backslash path get flagged. Bare program
    names and ``/usr/bin``/``/opt`` paths pass through.
"""

from __future__ import annotations

import json

import os
from pathlib import Path

import pytest

from puffo_agent.portal.state import (
    AGENT_INSTALLED_MARKER,
    HOST_SYNCED_MARKER,
    _looks_host_local_command,
    sync_host_claude_ai_state,
    sync_host_enabled_plugins,
    sync_host_gemini_mcp_servers,
    sync_host_gemini_skills,
    sync_host_mcp_servers,
    sync_host_plugins,
    sync_host_skills,
)


def _symlinks_available(tmp_path: Path) -> bool:
    """Probe: can this process create a symlink in ``tmp_path``?
    Mirrors the helper in ``test_host_credentials.py`` so the
    plugin-sync tests can skip the symlink path on Windows-without-
    Developer-Mode without dragging in a shared import.
    """
    probe = tmp_path / "_probe_symlink"
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


# ── Skills ───────────────────────────────────────────────────────────────────


def _write_skill(root, name, body="body", extra=None):
    """Create ``root/<name>/SKILL.md`` with optional supporting files."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, content in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


def test_sync_host_skills_copies_directory_form(tmp_path):
    host = tmp_path / "host"
    host_skills = host / ".claude" / "skills"
    _write_skill(host_skills, "one", body="A")
    _write_skill(
        host_skills, "two", body="B",
        extra={"reference.md": "ref", "scripts/helper.py": "print('x')"},
    )
    agent = tmp_path / "agent"

    n = sync_host_skills(host, agent)

    assert n == 2
    one = agent / ".claude" / "skills" / "one"
    two = agent / ".claude" / "skills" / "two"
    assert (one / "SKILL.md").read_text() == "A"
    assert (one / HOST_SYNCED_MARKER).exists()
    assert (two / "SKILL.md").read_text() == "B"
    assert (two / "reference.md").read_text() == "ref"
    assert (two / "scripts" / "helper.py").read_text() == "print('x')"
    assert (two / HOST_SYNCED_MARKER).exists()


def test_sync_host_skills_overwrites_existing_host_synced_dir(tmp_path):
    """Host updates a skill -> next sync picks up the new version and
    removes stale files from the old dir."""
    host = tmp_path / "host"
    _write_skill(host / ".claude" / "skills", "shared", body="v2")
    agent = tmp_path / "agent"
    # Simulate a previous sync with stale_file + old SKILL.md.
    agent_dir = agent / ".claude" / "skills" / "shared"
    agent_dir.mkdir(parents=True)
    (agent_dir / "SKILL.md").write_text("v1", encoding="utf-8")
    (agent_dir / "stale_file.md").write_text("old", encoding="utf-8")
    (agent_dir / HOST_SYNCED_MARKER).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert (agent_dir / "SKILL.md").read_text() == "v2"
    assert not (agent_dir / "stale_file.md").exists()
    assert (agent_dir / HOST_SYNCED_MARKER).exists()


def test_sync_host_skills_preserves_agent_installed_dirs(tmp_path):
    """A dir tagged ``agent-installed.md`` survives the host sync
    untouched, even on a name collision."""
    host = tmp_path / "host"
    _write_skill(host / ".claude" / "skills", "collides", body="H")
    _write_skill(host / ".claude" / "skills", "from_host", body="HOST_ONLY")

    agent = tmp_path / "agent"
    agent_skills = agent / ".claude" / "skills"
    # Edge case: agent-installed skill at user scope (normally these
    # live in workspace scope).
    agent_made = agent_skills / "collides"
    agent_made.mkdir(parents=True)
    (agent_made / "SKILL.md").write_text("AGENT", encoding="utf-8")
    (agent_made / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert (agent_made / "SKILL.md").read_text() == "AGENT"
    assert not (agent_made / HOST_SYNCED_MARKER).exists()
    assert (agent_skills / "from_host" / "SKILL.md").read_text() == "HOST_ONLY"


def test_sync_host_skills_prunes_removed_host_skills(tmp_path):
    """Host removed a skill -> previously synced copy is pruned, but
    only when we tagged it."""
    host = tmp_path / "host"
    (host / ".claude" / "skills").mkdir(parents=True)  # empty now
    agent = tmp_path / "agent"
    agent_skills = agent / ".claude" / "skills"
    # One dir tagged by us; one tagged by the agent; one untagged.
    for tag in (HOST_SYNCED_MARKER, AGENT_INSTALLED_MARKER, None):
        name = {
            HOST_SYNCED_MARKER: "was_host",
            AGENT_INSTALLED_MARKER: "agent_kept",
            None: "untagged",
        }[tag]
        d = agent_skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x", encoding="utf-8")
        if tag:
            (d / tag).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert not (agent_skills / "was_host").exists()
    assert (agent_skills / "agent_kept" / "SKILL.md").read_text() == "x"
    assert (agent_skills / "untagged" / "SKILL.md").read_text() == "x"


def test_sync_host_skills_ignores_flat_md_files(tmp_path):
    """Flat ``.md`` files at the top level aren't valid skills (format
    is ``<name>/SKILL.md``); sync skips them."""
    host = tmp_path / "host"
    (host / ".claude" / "skills").mkdir(parents=True)
    (host / ".claude" / "skills" / "orphan.md").write_text("x", encoding="utf-8")
    _write_skill(host / ".claude" / "skills", "real_skill", body="Y")
    agent = tmp_path / "agent"

    n = sync_host_skills(host, agent)

    assert n == 1
    assert (agent / ".claude" / "skills" / "real_skill" / "SKILL.md").read_text() == "Y"
    assert not (agent / ".claude" / "skills" / "orphan.md").exists()


def test_sync_host_skills_missing_host_dir_is_noop(tmp_path):
    host = tmp_path / "host"  # no .claude/skills/
    agent = tmp_path / "agent"
    assert sync_host_skills(host, agent) == 0
    # Don't create an empty dst when there was nothing to copy.
    assert not (agent / ".claude" / "skills").exists()


# ── MCP registrations ────────────────────────────────────────────────────────


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sync_host_mcp_merges_host_servers_into_empty_agent(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "server-fs"]},
        },
    })

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 1
    assert unreachable == []
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "npx"


def test_sync_host_mcp_preserves_agent_only_entries(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "fs": {"command": "npx", "args": []},
        },
    })
    _write_json(agent / ".claude.json", {
        "mcpServers": {
            "agent-only": {"command": "python3", "args": ["/workspace/a.py"]},
        },
        "somethingElse": {"keep": "me"},
    })

    merged, _ = sync_host_mcp_servers(host, agent)

    assert merged == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    # both entries present
    assert set(data["mcpServers"].keys()) == {"fs", "agent-only"}
    # unrelated top-level keys preserved
    assert data["somethingElse"] == {"keep": "me"}


def test_sync_host_mcp_host_wins_on_collision(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "shared": {"command": "npx", "args": ["host-version"]},
        },
    })
    _write_json(agent / ".claude.json", {
        "mcpServers": {
            "shared": {"command": "npx", "args": ["agent-version"]},
        },
    })

    sync_host_mcp_servers(host, agent)

    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["shared"]["args"] == ["host-version"]


def test_sync_host_mcp_flags_unreachable_command_paths(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "bare-ok": {"command": "npx", "args": []},
            "mac-local": {"command": "/Users/alice/bin/mcp", "args": []},
            "linux-home": {"command": "/home/bob/mcp", "args": []},
            "windows": {"command": r"C:\Users\bob\mcp.exe", "args": []},
            "container-ok": {"command": "/home/agent/.local/bin/mcp", "args": []},
            "sys-ok": {"command": "/usr/local/bin/node", "args": []},
        },
    })

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 6
    flagged_names = sorted(name for name, _ in unreachable)
    assert flagged_names == ["linux-home", "mac-local", "windows"]


def test_sync_host_mcp_no_host_file_is_noop(tmp_path):
    host = tmp_path / "host"  # no .claude.json
    agent = tmp_path / "agent"
    merged, unreachable = sync_host_mcp_servers(host, agent)
    assert merged == 0
    assert unreachable == []
    assert not (agent / ".claude.json").exists()


def test_sync_host_mcp_empty_host_servers_is_noop(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {"mcpServers": {}})
    _write_json(agent / ".claude.json", {"mcpServers": {"keep": {"command": "npx"}}})

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 0
    assert unreachable == []
    # Agent file untouched.
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"keep": {"command": "npx"}}}


def test_sync_host_mcp_handles_empty_agent_file(tmp_path):
    """``docker_cli.py`` touches ``.claude.json`` to a 0-byte file
    before ``docker run``. Merge must treat that as empty config."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {"fs": {"command": "npx"}},
    })
    (agent / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (agent / ".claude.json").touch()

    merged, _ = sync_host_mcp_servers(host, agent)

    assert merged == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert "fs" in data["mcpServers"]


# ── Plugins ──────────────────────────────────────────────────────────────────


def _populate_host_plugins(host: Path) -> Path:
    """Build a minimal host plugins/ tree that exercises every branch
    the agent's spawned Claude session will read. Returns the plugins
    dir path."""
    plugins = host / ".claude" / "plugins"
    (plugins / "marketplaces" / "claude-plugins-official" / ".git").mkdir(parents=True)
    (plugins / "marketplaces" / "claude-plugins-official" / "imessage" / "manifest.json").parent.mkdir(parents=True, exist_ok=True)
    (plugins / "marketplaces" / "claude-plugins-official" / "imessage" / "manifest.json").write_text(
        '{"name": "imessage"}', encoding="utf-8",
    )
    (plugins / "cache" / "build-001").mkdir(parents=True)
    (plugins / "cache" / "build-001" / "compiled.bin").write_bytes(b"\x00\x01\x02")
    (plugins / "installed_plugins.json").write_text(
        '{"plugins": ["imessage@claude-plugins-official"]}', encoding="utf-8",
    )
    (plugins / "known_marketplaces.json").write_text(
        '{"marketplaces": ["claude-plugins-official"]}', encoding="utf-8",
    )
    return plugins


def test_sync_host_plugins_symlinks_when_supported(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    _populate_host_plugins(host)
    agent = tmp_path / "agent"

    mode = sync_host_plugins(host, agent)

    assert mode == "symlink"
    plugins = agent / ".claude" / "plugins"
    assert plugins.is_symlink()
    # Resolving through the symlink lands at the host file — the
    # whole point: a fresh host plugin install shows up immediately.
    assert (plugins / "installed_plugins.json").read_text() == (
        '{"plugins": ["imessage@claude-plugins-official"]}'
    )
    assert (
        plugins / "marketplaces" / "claude-plugins-official" / "imessage" / "manifest.json"
    ).read_text() == '{"name": "imessage"}'


def test_sync_host_plugins_idempotent_when_symlink_exists(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    _populate_host_plugins(host)
    agent = tmp_path / "agent"

    assert sync_host_plugins(host, agent) == "symlink"
    # Second call recognises the existing symlink without re-creating.
    assert sync_host_plugins(host, agent) == "symlink (already)"


def test_sync_host_plugins_no_host_dir_returns_no_host_dir(tmp_path):
    host = tmp_path / "host"  # no .claude/plugins/
    agent = tmp_path / "agent"

    mode = sync_host_plugins(host, agent)

    assert mode == "no-host-dir"
    assert not (agent / ".claude" / "plugins").exists()


def test_sync_host_plugins_copy_fallback_when_symlink_blocked(
    tmp_path, monkeypatch,
):
    """Force the symlink branch to raise so the copytree fallback
    runs. Mirrors the Windows-without-Developer-Mode reality."""
    host = tmp_path / "host"
    _populate_host_plugins(host)
    agent = tmp_path / "agent"
    from puffo_agent.portal import state as state_mod

    def _no_symlink(*_args, **_kwargs):
        raise OSError("symlink blocked")
    monkeypatch.setattr(state_mod.os, "symlink", _no_symlink)

    mode = sync_host_plugins(host, agent)

    assert mode == "copy"
    plugins = agent / ".claude" / "plugins"
    assert plugins.is_dir() and not plugins.is_symlink()
    # Copy preserves the marketplace + cache + the two json siblings.
    assert (plugins / "installed_plugins.json").exists()
    assert (plugins / "known_marketplaces.json").exists()
    assert (
        plugins / "marketplaces" / "claude-plugins-official" / "imessage" / "manifest.json"
    ).read_text() == '{"name": "imessage"}'


def test_sync_host_plugins_copy_fresh_skips_recopy(tmp_path, monkeypatch):
    """An already-copied tree stays as-is — re-copying a GB-scale
    plugin tree on every worker tick would be the wrong default."""
    host = tmp_path / "host"
    _populate_host_plugins(host)
    agent = tmp_path / "agent"
    from puffo_agent.portal import state as state_mod
    monkeypatch.setattr(state_mod.os, "symlink", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope")))

    assert sync_host_plugins(host, agent) == "copy"
    # Modify host after the initial copy.
    (host / ".claude" / "plugins" / "new-marker").write_text("v2", encoding="utf-8")

    # Second call sees an existing dir and doesn't recopy.
    assert sync_host_plugins(host, agent) == "copy (fresh)"
    assert not (agent / ".claude" / "plugins" / "new-marker").exists()


# ── enabledPlugins propagation ───────────────────────────────────────────────


def test_sync_host_enabled_plugins_propagates_dict_shape(tmp_path):
    """Newer Claude Code stores enabledPlugins as
    ``{name: bool}``. Pass through unchanged."""
    host = tmp_path / "host"
    _write_json(host / ".claude" / "settings.json", {
        "enabledPlugins": {
            "imessage@claude-plugins-official": True,
            "chrome-devtools-mcp@claude-plugins-official": True,
        },
        "theme": "dark",  # unrelated key, must survive
    })
    agent = tmp_path / "agent"

    n = sync_host_enabled_plugins(host, agent)

    assert n == 2
    data = json.loads((agent / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == {
        "imessage@claude-plugins-official": True,
        "chrome-devtools-mcp@claude-plugins-official": True,
    }


def test_sync_host_enabled_plugins_propagates_list_shape(tmp_path):
    """Older Claude Code uses ``[name, ...]``. Same passthrough."""
    host = tmp_path / "host"
    _write_json(host / ".claude" / "settings.json", {
        "enabledPlugins": [
            "imessage@claude-plugins-official",
            "chrome-devtools-mcp@claude-plugins-official",
        ],
    })
    agent = tmp_path / "agent"

    n = sync_host_enabled_plugins(host, agent)

    assert n == 2
    data = json.loads((agent / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == [
        "imessage@claude-plugins-official",
        "chrome-devtools-mcp@claude-plugins-official",
    ]


def test_sync_host_enabled_plugins_preserves_other_agent_keys(tmp_path):
    host = tmp_path / "host"
    _write_json(host / ".claude" / "settings.json", {
        "enabledPlugins": {"foo@market": True},
    })
    agent = tmp_path / "agent"
    _write_json(agent / ".claude" / "settings.json", {
        "theme": "light",                  # agent-only preference
        "model": "claude-opus-4-7",        # agent-only preference
        "enabledPlugins": {"stale@old": True},  # to be overwritten
    })

    n = sync_host_enabled_plugins(host, agent)

    assert n == 1
    data = json.loads((agent / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["theme"] == "light"
    assert data["model"] == "claude-opus-4-7"
    # Stale agent entry replaced by host.
    assert data["enabledPlugins"] == {"foo@market": True}


def test_sync_host_enabled_plugins_creates_agent_settings_if_missing(tmp_path):
    """First-time agent has no settings.json yet (seed_claude_home
    hasn't run, or the operator-installed plugins haven't propagated).
    The helper still writes a minimal file."""
    host = tmp_path / "host"
    _write_json(host / ".claude" / "settings.json", {
        "enabledPlugins": {"foo@market": True},
    })
    agent = tmp_path / "agent"  # no settings.json

    n = sync_host_enabled_plugins(host, agent)

    assert n == 1
    data = json.loads((agent / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data == {"enabledPlugins": {"foo@market": True}}


def test_sync_host_enabled_plugins_noop_when_host_missing(tmp_path):
    host = tmp_path / "host"  # no settings.json
    agent = tmp_path / "agent"

    n = sync_host_enabled_plugins(host, agent)

    assert n == 0
    assert not (agent / ".claude" / "settings.json").exists()


def test_sync_host_enabled_plugins_noop_when_host_field_absent(tmp_path):
    """Host settings.json exists but has no enabledPlugins — leave
    the agent file alone (no spurious write that bumps mtime)."""
    host = tmp_path / "host"
    _write_json(host / ".claude" / "settings.json", {"theme": "dark"})
    agent = tmp_path / "agent"
    _write_json(agent / ".claude" / "settings.json", {"model": "claude-opus-4-7"})

    n = sync_host_enabled_plugins(host, agent)

    assert n == 0
    data = json.loads((agent / ".claude" / "settings.json").read_text(encoding="utf-8"))
    # Untouched.
    assert data == {"model": "claude-opus-4-7"}


# ── Unreachable-command heuristic ────────────────────────────────────────────


def test_looks_host_local_command_passes_bare_names():
    for cmd in ("npx", "node", "python3", "uvx", "bash"):
        assert not _looks_host_local_command(cmd)


def test_looks_host_local_command_passes_container_paths():
    for cmd in (
        "/usr/bin/node",
        "/usr/local/bin/python3",
        "/opt/puffoagent-pkg/puffoagent/mcp/puffo_core_server.py",
        "/home/agent/.local/bin/whatever",
        "/bin/sh",
    ):
        assert not _looks_host_local_command(cmd)


def test_looks_host_local_command_flags_host_paths():
    for cmd in (
        "/Users/alice/bin/mcp",
        "/home/bob/.local/bin/mcp",
        "/tmp/adhoc-server",
        "/var/folders/xy/T/mcp-12345",
        r"C:\Users\bob\mcp.exe",
        r"D:\apps\mcp.exe",
        "node C:\\stuff\\x.js",  # any backslash anywhere
    ):
        assert _looks_host_local_command(cmd), f"expected flagged: {cmd!r}"


def test_looks_host_local_command_empty_is_not_flagged():
    assert not _looks_host_local_command("")


# ── cli-local adapter integration ────────────────────────────────────────────


def _build_local_adapter(tmp_path, monkeypatch):
    """Construct a LocalCLIAdapter with ``Path.home()`` redirected to
    ``tmp_path/host`` and the ``claude`` binary check mocked. Returns
    (adapter, host, agent_home).
    """
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    agent_home = tmp_path / "agent" / "home"
    # Path.home() reads HOME on POSIX, USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("USERPROFILE", str(host))
    from puffo_agent.agent.adapters import local_cli
    monkeypatch.setattr(local_cli.shutil, "which", lambda _: "/fake/claude")
    adapter = local_cli.LocalCLIAdapter(
        agent_id="t",
        model="",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "sess.json"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        agent_home_dir=str(agent_home),
    )
    return adapter, host, agent_home


def test_local_cli_verify_syncs_host_skills(tmp_path, monkeypatch):
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_skill(host / ".claude" / "skills", "s1", body="SKILL")

    adapter._verify()

    assert (agent_home / ".claude" / "skills" / "s1" / "SKILL.md").read_text() == "SKILL"
    assert (agent_home / ".claude" / "skills" / "s1" / HOST_SYNCED_MARKER).exists()


def test_local_cli_verify_merges_host_mcp_servers(tmp_path, monkeypatch):
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_json(host / ".claude.json", {"mcpServers": {"fs": {"command": "npx"}}})

    adapter._verify()

    data = json.loads((agent_home / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "npx"


def test_local_cli_verify_does_not_warn_on_host_local_mcp(
    tmp_path, monkeypatch, caplog,
):
    """On cli-local the agent runs on the host, so host-local MCP
    command paths WILL resolve. The unreachable warning is docker-only.
    """
    import logging
    adapter, host, _agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "mac-local": {"command": "/Users/alice/bin/mcp"},
            "win-local": {"command": r"C:\Users\bob\mcp.exe"},
        },
    })

    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.local_cli"):
        adapter._verify()

    # No "host-local" warning. (Dangerous-mode warning at end of
    # _verify() is expected and filtered out.)
    offending = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "host-local" in r.message
    ]
    assert offending == []


def test_local_cli_verify_preserves_agent_installed_content(tmp_path, monkeypatch):
    """Skills/MCPs the agent registered for itself in a previous
    session survive the host sync on the next worker start."""
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    # Host has its own skill + MCP.
    _write_skill(host / ".claude" / "skills", "from_host", body="H")
    _write_json(host / ".claude.json", {
        "mcpServers": {"host-mcp": {"command": "npx"}},
    })
    # Agent already has an agent-installed skill + MCP with marker.
    agent_made = agent_home / ".claude" / "skills" / "agent_made"
    agent_made.mkdir(parents=True)
    (agent_made / "SKILL.md").write_text("A", encoding="utf-8")
    (agent_made / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")
    _write_json(agent_home / ".claude.json", {
        "mcpServers": {"agent-mcp": {"command": "python3"}},
    })

    adapter._verify()

    assert (agent_made / "SKILL.md").read_text() == "A"
    assert not (agent_made / HOST_SYNCED_MARKER).exists()
    assert (agent_home / ".claude" / "skills" / "from_host" / "SKILL.md").read_text() == "H"
    data = json.loads((agent_home / ".claude.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"].keys()) == {"agent-mcp", "host-mcp"}


# ── Gemini-side host sync ────────────────────────────────────────────────────


def test_sync_host_gemini_skills_copies_and_marks_for_provenance(tmp_path):
    """Mirrors the claude-code skill-sync contract for gemini: read
    from ``~/.gemini/skills/``, write to ``<agent_home>/.gemini/skills/``,
    drop a gemini-specific host-synced marker for provenance.
    """
    host = tmp_path / "host"
    host_skills = host / ".gemini" / "skills"
    _write_skill(host_skills, "pdf-reader", body="A")
    _write_skill(host_skills, "diagrammer", body="B")
    agent = tmp_path / "agent"

    n = sync_host_gemini_skills(host, agent)
    assert n == 2
    for name, body in (("pdf-reader", "A"), ("diagrammer", "B")):
        dst = agent / ".gemini" / "skills" / name
        assert (dst / "SKILL.md").read_text() == body
        marker = dst / HOST_SYNCED_MARKER
        assert marker.exists()
        # Marker must reference .gemini/ so it's distinguishable from
        # the claude host-sync marker.
        assert "~/.gemini/skills" in marker.read_text()


def test_sync_host_gemini_skills_preserves_agent_installed(tmp_path):
    """Agent-installed skills survive a host sync even on a name
    collision."""
    host = tmp_path / "host"
    _write_skill(host / ".gemini" / "skills", "mine", body="HOST")
    agent = tmp_path / "agent"
    made = agent / ".gemini" / "skills" / "mine"
    made.mkdir(parents=True)
    (made / "SKILL.md").write_text("AGENT", encoding="utf-8")
    (made / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")

    sync_host_gemini_skills(host, agent)

    assert (made / "SKILL.md").read_text() == "AGENT"
    assert (made / AGENT_INSTALLED_MARKER).exists()


def test_sync_host_gemini_skills_prunes_stale_host_synced(tmp_path):
    """Host-synced dirs the host removed get pruned; agent-installed
    dirs never do."""
    host = tmp_path / "host"
    _write_skill(host / ".gemini" / "skills", "fresh", body="F")
    agent = tmp_path / "agent"
    stale = agent / ".gemini" / "skills" / "gone"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("X", encoding="utf-8")
    (stale / HOST_SYNCED_MARKER).write_text("", encoding="utf-8")
    keep = agent / ".gemini" / "skills" / "mine"
    keep.mkdir(parents=True)
    (keep / "SKILL.md").write_text("A", encoding="utf-8")
    (keep / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")

    sync_host_gemini_skills(host, agent)

    assert not stale.exists()
    assert (keep / "SKILL.md").read_text() == "A"
    assert (agent / ".gemini" / "skills" / "fresh" / "SKILL.md").read_text() == "F"


def test_sync_host_gemini_mcp_servers_merges_host_entries(tmp_path):
    """Host ``mcpServers`` get merged into per-agent settings.json;
    agent-only entries survive; other top-level keys untouched."""
    host = tmp_path / "host"
    (host / ".gemini").mkdir(parents=True)
    (host / ".gemini" / "settings.json").write_text(json.dumps({
        "mcpServers": {"hmcp": {"command": "python3", "args": ["/srv/h.py"]}},
        "theme": "dark",
    }), encoding="utf-8")
    agent = tmp_path / "agent"
    (agent / ".gemini").mkdir(parents=True)
    (agent / ".gemini" / "settings.json").write_text(json.dumps({
        "mcpServers": {"amcp": {"command": "node", "args": ["/srv/a.js"]}},
        "context": {"fileName": ["GEMINI.md"]},
    }), encoding="utf-8")

    n, unreachable = sync_host_gemini_mcp_servers(host, agent)
    assert n == 1
    assert unreachable == []

    agent_data = json.loads((agent / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert set(agent_data["mcpServers"].keys()) == {"amcp", "hmcp"}
    # Non-mcpServers keys on per-agent settings are preserved.
    assert agent_data.get("context") == {"fileName": ["GEMINI.md"]}


def test_sync_host_gemini_mcp_servers_injects_extra_server_entry(tmp_path):
    """``extra_servers`` lets the adapter inject the puffo MCP entry in
    the same write, avoiding a race with a separate CLI registration."""
    host = tmp_path / "host"
    (host / ".gemini").mkdir(parents=True)
    (host / ".gemini" / "settings.json").write_text(json.dumps({
        "mcpServers": {"hmcp": {"command": "python3"}},
    }), encoding="utf-8")
    agent = tmp_path / "agent"

    puffo_entry = {
        "command": "python3",
        "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
        "env": {"PUFFO_AGENT_ID": "gbot"},
    }
    n, _ = sync_host_gemini_mcp_servers(
        host, agent, extra_servers={"puffo": puffo_entry},
    )
    assert n == 1  # host count doesn't include extras

    agent_data = json.loads((agent / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert set(agent_data["mcpServers"].keys()) == {"hmcp", "puffo"}
    assert agent_data["mcpServers"]["puffo"] == puffo_entry


def test_sync_host_gemini_mcp_servers_missing_host_file_is_noop(tmp_path):
    """No host settings.json: no merge, but ``extra_servers`` still
    writes through."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    n, _ = sync_host_gemini_mcp_servers(
        host, agent, extra_servers={"puffo": {"command": "python3"}},
    )
    assert n == 0
    agent_data = json.loads((agent / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert list(agent_data["mcpServers"].keys()) == ["puffo"]


def test_sync_host_gemini_mcp_servers_flags_host_local_commands(tmp_path):
    """Same heuristic as the claude-code path — absolute paths that
    won't resolve inside the container get flagged."""
    host = tmp_path / "host"
    (host / ".gemini").mkdir(parents=True)
    (host / ".gemini" / "settings.json").write_text(json.dumps({
        "mcpServers": {
            "local": {"command": "/Users/alice/.local/bin/weird"},
            "image": {"command": "python3"},
        },
    }), encoding="utf-8")
    agent = tmp_path / "agent"

    n, unreachable = sync_host_gemini_mcp_servers(host, agent)
    assert n == 2
    assert [name for name, _ in unreachable] == ["local"]


# ── sync_host_claude_ai_state (remote connector inheritance) ─────────────────


def test_sync_host_claude_ai_state_seeds_empty_agent(tmp_path):
    """Operator connected Gmail / Drive on the host side; a freshly-
    spawned agent must inherit the connector state. Without this the
    agent's Claude Code subprocess sees an empty ``claudeAi*`` block
    and ``ToolSearch`` can't find Gmail."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "claudeAiMcpEverConnected": [
            "claude.ai Gmail",
            "claude.ai Google Drive",
        ],
        "claudeAiAccount": {"email": "alice@example.com"},
        "mcpServers": {"local": {"command": "npx"}},
        "unrelated": {"keep": "me"},
    })

    n = sync_host_claude_ai_state(host, agent)

    assert n == 2  # 2 claudeAi* keys copied (NOT mcpServers, NOT unrelated)
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["claudeAiMcpEverConnected"] == [
        "claude.ai Gmail",
        "claude.ai Google Drive",
    ]
    assert data["claudeAiAccount"] == {"email": "alice@example.com"}
    # mcpServers + unrelated are NOT this sync's job — they should
    # NOT have been copied here.
    assert "mcpServers" not in data
    assert "unrelated" not in data


def test_sync_host_claude_ai_state_preserves_agent_state(tmp_path):
    """Agent's existing transcript / project / mcpServers state must
    survive a connector re-sync — only ``claudeAi*`` keys get
    overwritten."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "claudeAiMcpEverConnected": ["claude.ai Gmail"],
    })
    _write_json(agent / ".claude.json", {
        "mcpServers": {"puffo": {"command": "python"}},
        "projects": {"/some/path": {"history": ["..."]}},
        "claudeAiMcpEverConnected": ["claude.ai Notion"],  # stale
    })

    n = sync_host_claude_ai_state(host, agent)

    assert n == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    # Host wins on claudeAi* keys.
    assert data["claudeAiMcpEverConnected"] == ["claude.ai Gmail"]
    # Other state is untouched.
    assert data["mcpServers"]["puffo"]["command"] == "python"
    assert data["projects"]["/some/path"]["history"] == ["..."]


def test_sync_host_claude_ai_state_skips_oauth(tmp_path):
    """``claudeAiOauth`` is the auth blob — handled by
    ``link_host_credentials`` + the Keychain bridge, NOT here.
    Including it would race those paths."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "claudeAiOauth": {"accessToken": "sk-redacted"},
        "claudeAiMcpEverConnected": ["claude.ai Gmail"],
    })

    n = sync_host_claude_ai_state(host, agent)

    assert n == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["claudeAiMcpEverConnected"] == ["claude.ai Gmail"]
    # OAuth blob deliberately not copied.
    assert "claudeAiOauth" not in data


def test_sync_host_claude_ai_state_no_host_file(tmp_path):
    """No host file → noop, no agent file created."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    n = sync_host_claude_ai_state(host, agent)
    assert n == 0
    assert not (agent / ".claude.json").exists()


def test_sync_host_claude_ai_state_no_claude_ai_keys(tmp_path):
    """Host has a .claude.json but no claudeAi* keys → noop."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {"local": {"command": "npx"}},
    })
    n = sync_host_claude_ai_state(host, agent)
    assert n == 0
