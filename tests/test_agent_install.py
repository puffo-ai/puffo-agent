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
    _uninstall_mcp_server,
    _uninstall_skill,
    _write_refresh_flag,
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

    assert entries == [("system", "sys-mcp"), ("agent", "agent-mcp")]


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

    assert entries == [("agent", "agent-mcp")]


# ── refresh.flag ─────────────────────────────────────────────────────────────


def test_write_refresh_flag_no_model(tmp_path):
    path = _write_refresh_flag(tmp_path, None)

    assert path == tmp_path / ".puffo-agent" / "refresh.flag"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "requested_at" in payload
    assert "model" not in payload


def test_write_refresh_flag_with_model(tmp_path):
    path = _write_refresh_flag(tmp_path, "claude-opus-4-6")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model"] == "claude-opus-4-6"


def test_write_refresh_flag_with_empty_string_clears_model(tmp_path):
    """Explicit empty-string override = "use daemon default": the
    worker sets ``adapter.model = ""`` which drops ``--model`` on
    the next spawn."""
    path = _write_refresh_flag(tmp_path, "")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model"] == ""


def test_write_refresh_flag_rejects_non_string_model(tmp_path):
    with pytest.raises(RuntimeError, match="must be a string"):
        _write_refresh_flag(tmp_path, 42)  # type: ignore[arg-type]


# ── worker: _refresh_from_disk ──────────────────────────────────────────────


class _FakeAdapter:
    """Records reload() calls; exposes a mutable ``model`` attribute
    like the real LocalCLI / DockerCLI / SDK adapters.
    """
    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model = model
        self.reload_calls: list[str] = []

    async def reload(self, new_system_prompt: str) -> None:
        self.reload_calls.append(new_system_prompt)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_refresh_from_disk_tears_down_and_deletes_flag(tmp_path):
    from puffo_agent.portal.worker import _refresh_from_disk
    adapter = _FakeAdapter()
    flag_path = _write_refresh_flag(tmp_path, None)

    _run(_refresh_from_disk(
        agent_id="t", adapter=adapter, flag_path=flag_path,
    ))

    assert adapter.reload_calls == [""]
    # Model untouched (no override in flag).
    assert adapter.model == "claude-sonnet-4-6"
    assert not flag_path.exists()


def test_refresh_from_disk_applies_model_override(tmp_path):
    from puffo_agent.portal.worker import _refresh_from_disk
    adapter = _FakeAdapter(model="claude-sonnet-4-6")
    flag_path = _write_refresh_flag(tmp_path, "claude-opus-4-6")

    _run(_refresh_from_disk(
        agent_id="t", adapter=adapter, flag_path=flag_path,
    ))

    assert adapter.model == "claude-opus-4-6"
    assert adapter.reload_calls == [""]


def test_refresh_from_disk_empty_model_clears_override(tmp_path):
    from puffo_agent.portal.worker import _refresh_from_disk
    adapter = _FakeAdapter(model="claude-opus-4-6")
    flag_path = _write_refresh_flag(tmp_path, "")

    _run(_refresh_from_disk(
        agent_id="t", adapter=adapter, flag_path=flag_path,
    ))

    # Empty -> respawn omits --model and falls back to daemon default.
    assert adapter.model == ""


def test_refresh_from_disk_deletes_flag_even_on_adapter_failure(tmp_path):
    """Flag must be removed even on reload() failure, otherwise the
    worker would retry forever on every subsequent message."""
    from puffo_agent.portal.worker import _refresh_from_disk

    class _BrokenAdapter(_FakeAdapter):
        async def reload(self, _):
            raise RuntimeError("boom")

    flag_path = _write_refresh_flag(tmp_path, None)
    adapter = _BrokenAdapter()

    _run(_refresh_from_disk(
        agent_id="t", adapter=adapter, flag_path=flag_path,
    ))

    assert not flag_path.exists()


def test_refresh_from_disk_tolerates_corrupt_flag(tmp_path):
    """Malformed JSON: treat as 'no model override' and still trigger
    the restart."""
    from puffo_agent.portal.worker import _refresh_from_disk
    flag_path = tmp_path / ".puffo-agent" / "refresh.flag"
    flag_path.parent.mkdir(parents=True)
    flag_path.write_text("{not json", encoding="utf-8")
    adapter = _FakeAdapter(model="claude-sonnet-4-6")

    _run(_refresh_from_disk(
        agent_id="t", adapter=adapter, flag_path=flag_path,
    ))

    assert adapter.reload_calls == [""]
    assert adapter.model == "claude-sonnet-4-6"
    assert not flag_path.exists()
