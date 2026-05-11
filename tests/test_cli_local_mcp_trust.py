"""cli-local must bypass claude-code's per-project trust dialog so
MCP servers supplied via ``--mcp-config`` actually register.

Background: claude-code stores per-project trust state in
``~/.claude.json`` under ``projects[<cwd>].hasTrustDialogAccepted``.
A project that hasn't accepted the trust dialog gets its MCP
servers silently dropped — claude shows a TUI dialog interactively,
but ``--input-format stream-json`` has no surface for accepting,
so the dialog never resolves.

``--dangerously-skip-permissions`` bypasses BOTH the per-tool
approval prompt AND the trust dialog. ``--permission-mode
bypassPermissions`` only bypasses the per-tool prompt, leaving the
trust dialog blocking MCP registration. This file pins the cli-
local adapter to ``--dangerously-skip-permissions`` for the
``bypassPermissions`` mode so it matches cli-docker (which already
uses the right flag).

Unlike ``test_permission_mode.py`` (module-skipped pending the
permission-DM flow), this file is *always* exercised — the bypass
case is the one path cli-local actually serves today.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter


def _make_adapter(permission_mode: str = "bypassPermissions") -> LocalCLIAdapter:
    return LocalCLIAdapter(
        agent_id="a",
        model="claude-opus-4-7",
        workspace_dir="/tmp/ws",
        claude_dir="/tmp/ws/.claude",
        session_file="/tmp/a/cli_session.json",
        mcp_config_file="/tmp/a/mcp-config.json",
        agent_home_dir="/tmp/a",
        permission_mode=permission_mode,
    )


def test_bypass_mode_uses_dangerously_skip_permissions():
    adapter = _make_adapter("bypassPermissions")
    cmd = adapter._build_command(extra_args=[])
    assert "--dangerously-skip-permissions" in cmd
    assert "--permission-mode" not in cmd


def test_dangerously_skip_appears_before_extra_args():
    # Order matters: claude-code parses left to right and
    # --dangerously-skip-permissions has to be set before --mcp-config
    # is interpreted so the trust check is bypassed when the MCP
    # servers are loaded.
    adapter = _make_adapter("bypassPermissions")
    cmd = adapter._build_command(extra_args=["--mcp-config", "/some/path.json"])
    idx_skip = cmd.index("--dangerously-skip-permissions")
    idx_mcp = cmd.index("--mcp-config")
    assert idx_skip < idx_mcp


def test_model_flag_still_emitted():
    adapter = _make_adapter("bypassPermissions")
    cmd = adapter._build_command(extra_args=[])
    assert "--model" in cmd
    assert "claude-opus-4-7" in cmd


def test_command_shape_matches_cli_docker_for_bypass():
    # cli-docker emits ``claude --dangerously-skip-permissions --model
    # <model> ...``. cli-local in bypassPermissions mode should
    # produce the same shape on the claude argv (the docker prefix
    # is irrelevant here).
    adapter = _make_adapter("bypassPermissions")
    cmd = adapter._build_command(extra_args=["--verbose"])
    assert cmd[0] == "claude"
    assert cmd[1] == "--dangerously-skip-permissions"
    # --model + value sit right after the bypass flag.
    assert cmd[2] == "--model"
    assert cmd[3] == "claude-opus-4-7"
    assert "--verbose" in cmd[4:]
