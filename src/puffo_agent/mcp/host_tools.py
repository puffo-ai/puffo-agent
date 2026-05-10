"""Host-side helpers for MCP servers.

These tools manage the runtime host the agent is executing on —
skill / MCP-server install under ``.claude/`` and ``.mcp.json``,
asking the daemon to respawn the claude subprocess, etc.

Kept stdlib-only (no ``aiohttp`` / ``mcp`` imports) so this module
runs unchanged on the host (cli-local) and bind-mounted inside a
cli-docker container.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Provenance markers dropped inside every skill directory. Claude
# Code only executes ``SKILL.md``, so siblings are inert unless
# referenced from there — safe to use as tags.
AGENT_INSTALLED_MARKER = "agent-installed.md"
HOST_SYNCED_MARKER = "host-synced.md"
_AGENT_INSTALLED_BODY = (
    "This skill was installed by the agent via the install_skill "
    "MCP tool. It lives at project scope and survives host syncs.\n"
)

# Command-path prefixes that won't resolve inside a puffo-agent
# runtime container. Duplicated locally to keep this module
# stdlib-only.
_HOST_LOCAL_PREFIXES = ("/Users/", "/tmp/", "/var/folders/")


def _looks_host_local_command(command: str) -> bool:
    """True when ``command`` points at a host-specific path that
    won't exist inside a runtime container. Bare program names
    (``npx``, ``uvx``, ``python3``) pass through.
    """
    if not command:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", command) or "\\" in command:
        return True
    if command.startswith("/home/") and not command.startswith("/home/agent/"):
        return True
    return any(command.startswith(p) for p in _HOST_LOCAL_PREFIXES)


# ── Skill / MCP install helpers ─────────────────────────────────────────────
# Module-level so tests can drive them without standing up a FastMCP
# server.


def _workspace_skills_dir(workspace: Path) -> Path:
    """Project-scope skills dir."""
    return workspace / ".claude" / "skills"


def _system_skills_dir(home: Path) -> Path:
    """User-scope skills dir — operator-managed, host-synced."""
    return home / ".claude" / "skills"


def _workspace_mcp_path(workspace: Path) -> Path:
    """Project-scope MCP config (``.mcp.json``)."""
    return workspace / ".mcp.json"


def _system_claude_json_path(home: Path) -> Path:
    """User-scope claude config — contains system-scope MCPs."""
    return home / ".claude.json"


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"existing config at {path} is malformed JSON: {exc}. "
            "fix or delete the file before retrying."
        ) from exc
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _install_skill(workspace: Path, name: str, content: str) -> Path:
    if not _SKILL_NAME_RE.match(name or ""):
        raise RuntimeError(
            f"invalid skill name {name!r}: must be lowercase letters, "
            "digits, and hyphens (max 64 chars, can't start with a hyphen)"
        )
    if not content or not content.strip():
        raise RuntimeError("skill content is empty")
    dst = _workspace_skills_dir(workspace) / name
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "SKILL.md").write_text(content, encoding="utf-8")
    (dst / AGENT_INSTALLED_MARKER).write_text(
        _AGENT_INSTALLED_BODY, encoding="utf-8",
    )
    return dst


def _uninstall_skill(workspace: Path, name: str) -> Path:
    if not _SKILL_NAME_RE.match(name or ""):
        raise RuntimeError(f"invalid skill name {name!r}")
    dst = _workspace_skills_dir(workspace) / name
    if not dst.is_dir():
        raise RuntimeError(
            f"no agent-installed skill {name!r} at {dst}. "
            "use list_skills() to see what's available."
        )
    if not (dst / AGENT_INSTALLED_MARKER).exists():
        raise RuntimeError(
            f"skill {name!r} at {dst} has no {AGENT_INSTALLED_MARKER} "
            "marker — refusing to delete to avoid clobbering "
            "operator-managed content."
        )
    shutil.rmtree(dst)
    return dst


def _list_skills(workspace: Path, home: Path) -> list[tuple[str, str]]:
    """Return ``[(scope, name), ...]`` where scope is ``"system"``
    or ``"agent"``, sorted by scope then name."""
    out: list[tuple[str, str]] = []
    sysroot = _system_skills_dir(home)
    if sysroot.is_dir():
        for d in sorted(sysroot.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                out.append(("system", d.name))
    agentroot = _workspace_skills_dir(workspace)
    if agentroot.is_dir():
        for d in sorted(agentroot.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                out.append(("agent", d.name))
    return out


def _install_mcp_server(
    workspace: Path,
    name: str,
    command: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    check_host_local: bool = True,
) -> Path:
    """Install a project-scope MCP server.

    Set ``check_host_local`` True when the agent runs in a different
    filesystem from the operator (cli-docker, sdk-in-container) —
    host paths like ``/Users/alice/bin/mcp`` won't resolve there.
    False for cli-local where host paths DO resolve.
    """
    if not name or not isinstance(name, str) or len(name) > 64:
        raise RuntimeError(
            f"invalid MCP server name {name!r}: required, string, max 64 chars"
        )
    if not command or not isinstance(command, str):
        raise RuntimeError("command is required")
    if check_host_local and _looks_host_local_command(command):
        raise RuntimeError(
            f"refusing to register command {command!r}: looks host-local "
            "(absolute path that won't resolve inside the runtime). Use "
            "a bare program name (npx, uvx, python3) or an absolute "
            "path that exists in the container."
        )
    path = _workspace_mcp_path(workspace)
    data = _read_json_or_empty(path)
    servers = dict(data.get("mcpServers") or {})
    servers[name] = {
        "command": command,
        "args": list(args or []),
        "env": dict(env or {}),
    }
    data["mcpServers"] = servers
    _atomic_write_json(path, data)
    return path


def _uninstall_mcp_server(workspace: Path, name: str) -> Path:
    if not name or not isinstance(name, str):
        raise RuntimeError("name is required")
    path = _workspace_mcp_path(workspace)
    if not path.exists():
        raise RuntimeError(
            f"no project-scope MCP config at {path}. nothing to remove."
        )
    data = _read_json_or_empty(path)
    servers = dict(data.get("mcpServers") or {})
    if name not in servers:
        raise RuntimeError(
            f"no agent-installed MCP server {name!r} at project scope. "
            "use list_mcp_servers() to see what's available. system MCPs "
            "can't be removed from here."
        )
    servers.pop(name)
    data["mcpServers"] = servers
    _atomic_write_json(path, data)
    return path


def _list_mcp_servers(workspace: Path, home: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        sys_data = _read_json_or_empty(_system_claude_json_path(home))
    except RuntimeError:
        sys_data = {}
    for n in sorted((sys_data.get("mcpServers") or {}).keys()):
        out.append(("system", n))
    try:
        agent_data = _read_json_or_empty(_workspace_mcp_path(workspace))
    except RuntimeError:
        agent_data = {}
    for n in sorted((agent_data.get("mcpServers") or {}).keys()):
        out.append(("agent", n))
    return out


def _write_refresh_flag(workspace: Path, model: Optional[str]) -> Path:
    """Drop the refresh-flag file the worker watches on next turn.
    ``model``: None = no override, non-empty = switch to that model,
    ``""`` = clear back to the daemon default."""
    payload: dict[str, Any] = {"requested_at": int(time.time())}
    if model is not None:
        if not isinstance(model, str):
            raise RuntimeError("model must be a string (or omitted)")
        payload["model"] = model.strip()
    flag_path = workspace / ".puffo-agent" / "refresh.flag"
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not write refresh flag: {exc}") from exc
    return flag_path
