"""Tests for agent-scoped skill + MCP install/uninstall/list and the
refresh flag.

Exercises the module-level helpers directly; the ``@mcp.tool()``
wrappers in ``build_server`` are thin shims that delegate here and
format a human-readable return string.

Contract:
  * install_skill writes ``<workspace>/.claude/skills/<name>/SKILL.md``
    plus an ``agent-installed.md`` marker; validates the name.
  * uninstall_skill refuses when the marker is missing (so it can't
    wipe a system skill) and when the skill doesn't exist.
  * list_skills tags each entry system/agent across user-scope (HOME)
    and project-scope (workspace).
  * install_mcp_server writes ``<workspace>/.mcp.json`` mcpServers
    entry; rejects host-local commands.
  * uninstall_mcp_server only touches project scope; system MCPs in
    ~/.claude.json can't be removed from here.
  * refresh.flag payload carries an optional model override.
"""

from __future__ import annotations

import json

import pytest

from puffo_agent.mcp.host_tools import (
    AGENT_INSTALLED_MARKER,
    HOST_SYNCED_MARKER,
    _install_mcp_server,
    _install_skill,
    _list_mcp_servers,
    _list_skills,
    _touch_refresh_flag,
    _uninstall_mcp_server,
    _uninstall_skill,
    _write_refresh_model_flag,
    _write_refresh_runtime_flag,
)


# ── install_skill ────────────────────────────────────────────────────────────


def test_install_skill_writes_expected_layout(tmp_path):
    dst = _install_skill(tmp_path, "explain-code", "---\nname: explain-code\n---\nBody")

    assert dst == tmp_path / ".claude" / "skills" / "explain-code"
    assert (dst / "SKILL.md").read_text() == "---\nname: explain-code\n---\nBody"
    assert (dst / AGENT_INSTALLED_MARKER).exists()


def test_install_skill_overwrites_existing_agent_skill(tmp_path):
    _install_skill(tmp_path, "s", "v1")
    _install_skill(tmp_path, "s", "v2")
    assert (tmp_path / ".claude" / "skills" / "s" / "SKILL.md").read_text() == "v2"


@pytest.mark.parametrize("bad_name", [
    "",
    "Bad-Name",   # uppercase
    "-leading",   # leading hyphen
    "has spaces",
    "has/slash",
    "x" * 65,     # over length cap
])
def test_install_skill_rejects_invalid_names(tmp_path, bad_name):
    with pytest.raises(RuntimeError, match="invalid skill name"):
        _install_skill(tmp_path, bad_name, "body")


def test_install_skill_rejects_empty_content(tmp_path):
    with pytest.raises(RuntimeError, match="empty"):
        _install_skill(tmp_path, "ok", "")
    with pytest.raises(RuntimeError, match="empty"):
        _install_skill(tmp_path, "ok", "   \n  ")


# ── uninstall_skill ──────────────────────────────────────────────────────────


def test_uninstall_skill_removes_agent_installed_dir(tmp_path):
    dst = _install_skill(tmp_path, "s", "body")
    assert dst.exists()

    _uninstall_skill(tmp_path, "s")

    assert not dst.exists()


def test_uninstall_skill_missing_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no agent-installed skill"):
        _uninstall_skill(tmp_path, "nope")


def test_uninstall_skill_refuses_without_marker(tmp_path):
    """No agent-installed.md marker -> dir is operator-managed or
    unknown provenance; refuse to delete."""
    dst = tmp_path / ".claude" / "skills" / "system-skill"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("system content", encoding="utf-8")
    (dst / HOST_SYNCED_MARKER).write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no agent-installed.md"):
        _uninstall_skill(tmp_path, "system-skill")
    assert (dst / "SKILL.md").exists()


def test_uninstall_skill_rejects_bad_name(tmp_path):
    with pytest.raises(RuntimeError, match="invalid skill name"):
        _uninstall_skill(tmp_path, "../../etc")


# ── list_skills ──────────────────────────────────────────────────────────────


def test_list_skills_tags_scope(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    _install_skill(workspace, "agent-one", "a")
    # Simulate host-sync result.
    sys_dir = home / ".claude" / "skills" / "sys-one"
    sys_dir.mkdir(parents=True)
    (sys_dir / "SKILL.md").write_text("s", encoding="utf-8")
    (sys_dir / HOST_SYNCED_MARKER).write_text("", encoding="utf-8")

    entries = _list_skills(workspace, home)

    assert entries == [("system", "sys-one"), ("agent", "agent-one")]


def test_list_skills_ignores_entries_without_skill_md(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    (workspace / ".claude" / "skills" / "broken").mkdir(parents=True)
    # No SKILL.md — not a valid skill.
    (home / ".claude" / "skills" / "also-broken").mkdir(parents=True)

    assert _list_skills(workspace, home) == []


def test_list_skills_empty_when_nothing_installed(tmp_path):
    assert _list_skills(tmp_path / "ws", tmp_path / "home") == []


# ── install_mcp_server ───────────────────────────────────────────────────────


def test_install_mcp_server_writes_project_scope_config(tmp_path):
    path = _install_mcp_server(
        tmp_path, "github", "npx", ["-y", "@gh/mcp"], {"GH_TOKEN": "x"},
    )

    assert path == tmp_path / ".mcp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["github"] == {
        "command": "npx",
        "args": ["-y", "@gh/mcp"],
        "env": {"GH_TOKEN": "x"},
    }


def test_install_mcp_server_merges_with_existing_entries(tmp_path):
    _install_mcp_server(tmp_path, "first", "npx", ["a"], {})
    _install_mcp_server(tmp_path, "second", "uvx", ["b"], {})

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"].keys()) == {"first", "second"}


def test_install_mcp_server_overwrites_same_name(tmp_path):
    _install_mcp_server(tmp_path, "x", "npx", ["v1"], {})
    _install_mcp_server(tmp_path, "x", "npx", ["v2"], {})

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["x"]["args"] == ["v2"]


@pytest.mark.parametrize("bad_command", [
    "/Users/alice/bin/mcp",
    "/home/bob/.local/bin/mcp",
    r"C:\Users\bob\mcp.exe",
    "/tmp/adhoc-server",
    "node C:\\stuff\\x.js",  # backslash anywhere
])
def test_install_mcp_server_rejects_host_local_commands(tmp_path, bad_command):
    with pytest.raises(RuntimeError, match="host-local"):
        _install_mcp_server(tmp_path, "x", bad_command)
    assert not (tmp_path / ".mcp.json").exists()


@pytest.mark.parametrize("host_command", [
    "/Users/alice/bin/mcp",
    "/home/bob/.local/bin/mcp",
    "/tmp/adhoc-server",
])
def test_install_mcp_server_accepts_host_paths_when_check_disabled(
    tmp_path, host_command,
):
    """cli-local bypasses the host-local check: the agent runs on the
    host, so any path the operator can execute works."""
    _install_mcp_server(tmp_path, "x", host_command, check_host_local=False)
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["x"]["command"] == host_command


@pytest.mark.parametrize("ok_command", [
    "npx",
    "uvx",
    "python3",
    "/usr/local/bin/node",
    "/home/agent/.local/bin/my-mcp",
])
def test_install_mcp_server_accepts_runtime_local_commands(tmp_path, ok_command):
    _install_mcp_server(tmp_path, "x", ok_command)
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["x"]["command"] == ok_command


def test_install_mcp_server_validates_name(tmp_path):
    with pytest.raises(RuntimeError, match="invalid MCP server name"):
        _install_mcp_server(tmp_path, "", "npx")
    with pytest.raises(RuntimeError, match="invalid MCP server name"):
        _install_mcp_server(tmp_path, "x" * 65, "npx")


def test_install_mcp_server_requires_command(tmp_path):
    with pytest.raises(RuntimeError, match="command is required"):
        _install_mcp_server(tmp_path, "x", "")


# ── uninstall_mcp_server ─────────────────────────────────────────────────────


def test_uninstall_mcp_server_removes_entry(tmp_path):
    _install_mcp_server(tmp_path, "a", "npx")
    _install_mcp_server(tmp_path, "b", "npx")

    _uninstall_mcp_server(tmp_path, "a")

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"].keys()) == {"b"}


def test_uninstall_mcp_server_missing_raises(tmp_path):
    _install_mcp_server(tmp_path, "a", "npx")
    with pytest.raises(RuntimeError, match="no agent-installed MCP server"):
        _uninstall_mcp_server(tmp_path, "nope")


def test_uninstall_mcp_server_no_config_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no project-scope MCP config"):
        _uninstall_mcp_server(tmp_path, "anything")


# ── list_mcp_servers ─────────────────────────────────────────────────────────


def test_list_mcp_servers_tags_scope(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    # System: host-installed in ~/.claude.json.
    (home).mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sys-mcp": {"command": "npx"}}}),
        encoding="utf-8",
    )
    # Agent: project-scope via install.
    _install_mcp_server(workspace, "agent-mcp", "uvx")

    entries = _list_mcp_servers(workspace, home)

    # 3-tuple shape: (scope, name, source). System + agent entries
    # leave ``source`` blank; only plugin entries fill it in (see
    # the plugin-scope tests below).
    assert entries == [
        ("system", "sys-mcp", ""),
        ("agent", "agent-mcp", ""),
    ]


def test_list_mcp_servers_empty_when_nothing_registered(tmp_path):
    assert _list_mcp_servers(tmp_path / "ws", tmp_path / "home") == []


def test_list_mcp_servers_tolerates_malformed_system_config(tmp_path):
    """Malformed ~/.claude.json must not crash listing — agents should
    still see their own MCPs."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".claude.json").write_text("{not json", encoding="utf-8")
    _install_mcp_server(workspace, "agent-mcp", "uvx")

    entries = _list_mcp_servers(workspace, home)

    assert entries == [("agent", "agent-mcp", "")]


# ── plugin-scope listing ────────────────────────────────────────


def _seed_plugin_mcp(home, plugin: str, version: str, servers: dict) -> None:
    """Drop a ``<home>/.claude/plugins/cache/<plugin>/<version>/.mcp.json``
    matching the shape claude-code's plugin loader writes."""
    plugin_dir = home / ".claude" / "plugins" / "cache" / plugin / version
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )


def test_list_mcp_servers_includes_plugin_scope(tmp_path):
    """Plugin-routed MCP servers live under
    ``~/.claude/plugins/cache/<plugin>/<version>/.mcp.json`` — a
    distinct scope from the system + agent paths. They must show
    up tagged ``plugin`` so the agent can call them and the
    operator can tell which plugin owns each entry. The agent-
    reported case that motivated this: imessage + chrome-devtools-
    mcp returning empty from ``list_mcp_servers`` despite being
    installed via ``claude /plugin install``."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _seed_plugin_mcp(home, "imessage", "0.1.0", {
        "imessage": {"command": "bun", "args": ["run"]},
    })
    _seed_plugin_mcp(home, "chrome-devtools-mcp", "0.22.0", {
        "chrome-devtools": {"command": "npx", "args": ["chrome-devtools-mcp@latest"]},
    })

    entries = _list_mcp_servers(workspace, home)

    # Sorted alphabetically by plugin name, then by server name
    # within a plugin. ``source`` carries the ``<plugin>/<version>``
    # label so the operator can match server back to plugin.
    assert entries == [
        ("plugin", "chrome-devtools", "chrome-devtools-mcp/0.22.0"),
        ("plugin", "imessage", "imessage/0.1.0"),
    ]


def test_list_mcp_servers_combines_all_three_scopes(tmp_path):
    """System / agent / plugin entries coexist; the listing emits
    them in scope order (system, agent, plugin) so the agent can
    eyeball "did my install land?" from the bottom up."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sys-mcp": {"command": "npx"}}}),
        encoding="utf-8",
    )
    _install_mcp_server(workspace, "agent-mcp", "uvx")
    _seed_plugin_mcp(home, "imessage", "0.1.0", {
        "imessage": {"command": "bun"},
    })

    entries = _list_mcp_servers(workspace, home)

    assert entries == [
        ("system", "sys-mcp", ""),
        ("agent", "agent-mcp", ""),
        ("plugin", "imessage", "imessage/0.1.0"),
    ]


def test_list_mcp_servers_skips_malformed_plugin_mcp_json(tmp_path):
    """One plugin with garbled JSON must not nuke the whole listing
    — the agent should still see every other plugin's servers.
    Defensive because plugin authors aren't operating under our
    quality bar and a half-written download / git pull could land
    a partial file."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    bad = home / ".claude" / "plugins" / "cache" / "bad-plugin" / "9.9.9"
    bad.mkdir(parents=True)
    (bad / ".mcp.json").write_text("{not json", encoding="utf-8")
    _seed_plugin_mcp(home, "good-plugin", "0.1.0", {
        "good-srv": {"command": "npx"},
    })

    entries = _list_mcp_servers(workspace, home)

    assert entries == [
        ("plugin", "good-srv", "good-plugin/0.1.0"),
    ]


def test_list_mcp_servers_skips_plugin_dirs_without_mcp_json(tmp_path):
    """A plugin can be installed without registering any MCP server
    (skills-only / hooks-only plugins are a real category). The
    plugin tree exists but has no ``.mcp.json`` — listing must
    treat the directory as empty for MCP purposes rather than
    crashing on the missing file."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    skills_only = home / ".claude" / "plugins" / "cache" / "skills-pkg" / "0.1.0"
    skills_only.mkdir(parents=True)
    # No .mcp.json in this dir — only SKILL.md, hooks, etc. would
    # normally live here.
    (skills_only / "README.md").write_text("docs", encoding="utf-8")
    _seed_plugin_mcp(home, "real-mcp", "0.1.0", {
        "srv": {"command": "npx"},
    })

    entries = _list_mcp_servers(workspace, home)

    assert entries == [
        ("plugin", "srv", "real-mcp/0.1.0"),
    ]


def test_list_mcp_servers_handles_multiple_versions(tmp_path):
    """A plugin can have several versions cached side-by-side after
    an upgrade dance; each version that ships a ``.mcp.json`` gets
    its own row so the agent + operator can see exactly which
    version's MCP server is actually live (or, in a half-upgraded
    state, that both are)."""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _seed_plugin_mcp(home, "imessage", "0.1.0", {
        "imessage": {"command": "bun"},
    })
    _seed_plugin_mcp(home, "imessage", "0.2.0", {
        "imessage": {"command": "bun"},
    })

    entries = _list_mcp_servers(workspace, home)

    assert entries == [
        ("plugin", "imessage", "imessage/0.1.0"),
        ("plugin", "imessage", "imessage/0.2.0"),
    ]


# ── codex harness routing ────────────────────────────────────────────────────


def test_list_mcp_servers_codex_reads_config_toml(tmp_path):
    """When harness=codex the listing pulls from
    ``<home>/.codex/config.toml`` — NOT ``.claude.json``. That's the
    file codex's CLI actually reads at boot, so it's the only honest
    answer for ""what can my agent call right now?""."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    # Seed the wrong tree first — codex listing must IGNORE this.
    home.mkdir()
    (home / ".claude.json").write_text(
        '{"mcpServers": {"red-herring": {"command": "x"}}}',
        encoding="utf-8",
    )
    # The actual codex config — what should surface.
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.coinbase-cdp]\n'
        'command = "npx"\n'
        'args = ["mcp-remote", "https://docs.cdp.coinbase.com/mcp"]\n'
        '\n'
        '[mcp_servers.puffo]\n'
        'command = "python"\n'
        'args = ["-m", "puffo_agent.mcp.puffo_core_server"]\n',
        encoding="utf-8",
    )
    entries = _list_mcp_servers(workspace, home, harness="codex")
    assert entries == [
        ("system", "coinbase-cdp", ""),
        ("system", "puffo", ""),
    ]


def test_list_mcp_servers_codex_empty_when_no_config_toml(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    assert _list_mcp_servers(workspace, home, harness="codex") == []


def test_list_mcp_servers_default_harness_keeps_claude_behavior(tmp_path):
    """Existing claude-code callers (harness omitted / empty string)
    keep reading .claude.json — no regression."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    home.mkdir()
    (home / ".claude.json").write_text(
        '{"mcpServers": {"gh": {"command": "x"}}}',
        encoding="utf-8",
    )
    entries = _list_mcp_servers(workspace, home)
    assert entries == [("system", "gh", "")]


# ── refresh_*.flag writers ──────────────────────────────────────────────────


def test_touch_refresh_flag_writes_worker_scope_payload(tmp_path):
    for name in ("refresh_agent", "refresh_host_sync", "refresh_session"):
        path = _touch_refresh_flag(tmp_path, name)
        assert path == tmp_path / ".puffo-agent" / f"{name}.flag"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload["requested_at"], int)


def test_touch_refresh_flag_rejects_daemon_scope_names(tmp_path):
    for name in ("refresh_model", "refresh_runtime", "made-up"):
        with pytest.raises(RuntimeError):
            _touch_refresh_flag(tmp_path, name)


def test_write_refresh_model_flag_carries_harness_and_model(tmp_path):
    path = _write_refresh_model_flag(
        tmp_path, harness="codex", model="gpt-5",
    )
    assert path == tmp_path / ".puffo-agent" / "refresh_model.flag"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["harness"] == "codex"
    assert payload["model"] == "gpt-5"
    assert isinstance(payload["requested_at"], int)


def test_write_refresh_runtime_flag_kind_only(tmp_path):
    path = _write_refresh_runtime_flag(tmp_path, kind="cli-local")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"kind": "cli-local", "requested_at": payload["requested_at"]}


def test_write_refresh_runtime_flag_with_swap(tmp_path):
    path = _write_refresh_runtime_flag(
        tmp_path,
        kind="cli-docker",
        harness="claude-code",
        model="claude-sonnet-4-6",
        provider="anthropic",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "cli-docker"
    assert payload["harness"] == "claude-code"
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["provider"] == "anthropic"


# ── worker: _process_refresh_flags ─────────────────────────────────────────


class _FakeAdapter:
    """Records reload() calls with the with_session kwarg."""

    def __init__(self):
        self.reload_calls: list[tuple[str, bool]] = []

    async def reload(
        self, new_system_prompt: str, *, with_session: bool = False,
    ) -> None:
        self.reload_calls.append((new_system_prompt, with_session))


class _FakePuffo:
    def __init__(self, prompt: str = "old prompt"):
        self.system_prompt = prompt


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_process_refresh_flags_noop_when_no_flags(tmp_path):
    from puffo_agent.portal.worker import _process_refresh_flags
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    _run(_process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=tmp_path / "refresh_agent.flag",
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",
    ))
    assert adapter.reload_calls == []


def test_process_refresh_flags_session_only(tmp_path, monkeypatch):
    """Only session flag set → adapter.reload with with_session=True."""
    from puffo_agent.portal.worker import _process_refresh_flags
    adapter = _FakeAdapter()
    puffo = _FakePuffo(prompt="preserved")
    session_flag = tmp_path / "refresh_session.flag"
    session_flag.write_text("{}", encoding="utf-8")

    _run(_process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=tmp_path / "refresh_agent.flag",
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=session_flag,
    ))
    assert len(adapter.reload_calls) == 1
    prompt, with_session = adapter.reload_calls[0]
    assert prompt == "preserved"
    assert with_session is True
    assert not session_flag.exists()


def test_process_refresh_flags_deletes_flags_after_processing(tmp_path, monkeypatch):
    from puffo_agent.portal import worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "_rebuild_managed_system_prompt",
        lambda **_: "new prompt",
    )
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    agent_flag = tmp_path / "refresh_agent.flag"
    agent_flag.write_text("{}", encoding="utf-8")

    _run(worker_mod._process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=agent_flag,
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",
    ))
    assert puffo.system_prompt == "new prompt"
    assert adapter.reload_calls == [("new prompt", True)]
    assert not agent_flag.exists()


def test_process_refresh_flags_prompt_rebuild_forces_fresh_session(tmp_path, monkeypatch):
    """A rebuild that changed the prompt drops the CLI session even without
    an explicit session flag — ``--resume`` would replay the stale baked
    system prompt."""
    from puffo_agent.portal import worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "_rebuild_managed_system_prompt", lambda **_: "rebuilt",
    )
    adapter = _FakeAdapter()
    puffo = _FakePuffo(prompt="stale")
    agent_flag = tmp_path / "refresh_agent.flag"
    agent_flag.write_text("{}", encoding="utf-8")

    _run(worker_mod._process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=agent_flag,
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",  # NOT set
    ))
    assert adapter.reload_calls == [("rebuilt", True)]


def test_process_refresh_flags_unchanged_rebuild_keeps_session(tmp_path, monkeypatch):
    """A rebuild that produced the SAME prompt preserves the conversation —
    only an actual prompt change forces the fresh session."""
    from puffo_agent.portal import worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "_rebuild_managed_system_prompt", lambda **_: "same",
    )
    adapter = _FakeAdapter()
    puffo = _FakePuffo(prompt="same")
    agent_flag = tmp_path / "refresh_agent.flag"
    agent_flag.write_text("{}", encoding="utf-8")

    _run(worker_mod._process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=agent_flag,
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",
    ))
    assert adapter.reload_calls == [("same", False)]
    assert not agent_flag.exists()


def test_process_refresh_flags_session_flag_wins_over_unchanged_rebuild(tmp_path, monkeypatch):
    """An explicit session flag drops the session even when the rebuilt
    prompt is unchanged."""
    from puffo_agent.portal import worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "_rebuild_managed_system_prompt", lambda **_: "same",
    )
    adapter = _FakeAdapter()
    puffo = _FakePuffo(prompt="same")
    agent_flag = tmp_path / "refresh_agent.flag"
    agent_flag.write_text("{}", encoding="utf-8")
    session_flag = tmp_path / "refresh_session.flag"
    session_flag.write_text("{}", encoding="utf-8")

    _run(worker_mod._process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=agent_flag,
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=session_flag,
    ))
    assert adapter.reload_calls == [("same", True)]
    assert not session_flag.exists()


def test_process_refresh_flags_rebuild_failure_keeps_session(tmp_path, monkeypatch):
    """If the rebuild raises, the old prompt reloads and the session is
    preserved (nothing changed, so nothing to force)."""
    from puffo_agent.portal import worker as worker_mod

    def _boom(**_):
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(worker_mod, "_rebuild_managed_system_prompt", _boom)
    adapter = _FakeAdapter()
    puffo = _FakePuffo(prompt="old prompt")
    agent_flag = tmp_path / "refresh_agent.flag"
    agent_flag.write_text("{}", encoding="utf-8")

    _run(worker_mod._process_refresh_flags(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=puffo,
        adapter=adapter,
        refresh_agent_flag=agent_flag,
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",
    ))
    assert adapter.reload_calls == [("old prompt", False)]
    assert not agent_flag.exists()
