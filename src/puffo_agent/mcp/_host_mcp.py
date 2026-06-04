"""Filesystem + catalog plumbing for install_host_mcp / sync_host_mcp.

Kept in a dedicated module so puffo_core_tools.py stays focused on
the MCP tool decorators. The two ``_impl`` entry points are awaited
straight from the tool bodies in puffo_core_tools.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .puffo_core_tools import PuffoCoreToolsConfig


def _read_claude_json(path: Path) -> dict[str, Any]:
    """Read a ``.claude.json``. Returns ``{}`` on missing / unreadable /
    non-object. Callers MUST treat an unreadable file as "leave it
    alone" — we don't want to clobber operator state on parse error.
    """
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _atomic_write_claude_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically: tmp file then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _spec_from_template(template: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a catalog ``mcp_template`` row into the
    ``mcpServers[<id>]`` shape Claude Code reads. Mirrors
    ``desired_install.normalize_mcp_spec`` for the same wire shape.
    """
    transport = str(template.get("type") or "").lower()
    args = template.get("args") or []
    env = template.get("env") or {}
    args_list = [str(a) for a in args] if isinstance(args, list) else []
    env_map = (
        {str(k): str(v) for k, v in env.items()}
        if isinstance(env, dict) else {}
    )
    if transport == "stdio":
        command = template.get("command")
        if not isinstance(command, str) or not command:
            return None
        return {
            "type": "stdio",
            "command": command,
            "args": args_list,
            "env": env_map,
        }
    if transport in ("sse", "http"):
        url = template.get("url")
        if not isinstance(url, str) or not url:
            return None
        return {"type": transport, "url": url, "env": env_map}
    return None


def _env_setup_lines(spec: dict[str, Any]) -> str:
    """Human-friendly lines listing env vars the operator needs to
    populate. Empty-value keys signal "this is the placeholder, fill
    me in". Returns an empty string when nothing is required.
    """
    env_map = spec.get("env") or {}
    missing = [k for k, v in env_map.items() if not str(v)]
    if not missing:
        return ""
    lines = ["The MCP needs these env values:"]
    for key in missing:
        lines.append(f"  - {key}")
    return "\n".join(lines)


async def _install_host_mcp_impl(
    cfg: "PuffoCoreToolsConfig", template_id: str,
) -> str:
    if not cfg.host_home:
        raise RuntimeError(
            "install_host_mcp unavailable — PUFFO_HOST_HOME not set "
            "on this MCP runtime (only cli-local supports host writes)."
        )
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("install_host_mcp: template_id is required")

    try:
        template = await cfg.http_client.get(
            f"/v2/mcp-templates/{template_id}"
        )
    except Exception as exc:
        raise RuntimeError(
            f"install_host_mcp: catalog fetch failed for "
            f"{template_id!r}: {exc}"
        ) from exc
    if not isinstance(template, dict):
        raise RuntimeError(
            f"install_host_mcp: catalog returned a non-object body "
            f"for {template_id!r}"
        )

    spec = _spec_from_template(template)
    if spec is None:
        raise RuntimeError(
            f"install_host_mcp: catalog entry for {template_id!r} has "
            f"an unsupported transport or is missing a required field"
        )

    host_claude_json = Path(cfg.host_home) / ".claude.json"
    data = _read_claude_json(host_claude_json)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if template_id in servers:
        return (
            f"{template_id!r} is already registered in the host's "
            f"~/.claude.json. Call sync_host_mcp({template_id!r}) to "
            f"copy the operator's populated entry into your own config."
        )
    servers[template_id] = spec
    data["mcpServers"] = servers
    _atomic_write_claude_json(host_claude_json, data)

    name = template.get("name") or template_id
    description = template.get("description") or ""
    setup_lines = _env_setup_lines(spec)
    op = f"@{cfg.operator_slug}" if cfg.operator_slug else "the operator"
    parts = [
        f"Installed {name} into your host ~/.claude.json as "
        f"mcpServers[{template_id!r}].",
    ]
    if description:
        parts.append(description)
    if setup_lines:
        parts.append(setup_lines)
        parts.append(
            "Complete the OAuth or paste the API key(s) on host (the "
            "MCP package's own setup flow), then ping me back."
        )
    else:
        parts.append("No env setup required — call sync_host_mcp next.")
    parts.append(
        f"Once host is ready: sync_host_mcp({template_id!r}) then "
        f"refresh()."
    )
    body = "\n\n".join(parts)
    return (
        f"DM {op} with the following message:\n\n---\n{body}\n---"
    )


async def _sync_host_mcp_impl(
    cfg: "PuffoCoreToolsConfig", template_id: str,
) -> str:
    if not cfg.host_home or not cfg.agent_home:
        raise RuntimeError(
            "sync_host_mcp unavailable — PUFFO_HOST_HOME / agent_home "
            "not set on this MCP runtime."
        )
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("sync_host_mcp: template_id is required")

    host_claude_json = Path(cfg.host_home) / ".claude.json"
    host_data = _read_claude_json(host_claude_json)
    host_servers = host_data.get("mcpServers")
    if not isinstance(host_servers, dict) or template_id not in host_servers:
        return (
            f"sync_host_mcp: no entry for {template_id!r} in the host's "
            f"~/.claude.json. Call install_host_mcp({template_id!r}) first "
            f"and DM the operator the setup steps."
        )
    entry = host_servers[template_id]

    agent_claude_json = Path(cfg.agent_home) / ".claude.json"
    agent_data = _read_claude_json(agent_claude_json)
    agent_servers = agent_data.get("mcpServers")
    if not isinstance(agent_servers, dict):
        agent_servers = {}
    agent_servers[template_id] = entry
    agent_data["mcpServers"] = agent_servers
    _atomic_write_claude_json(agent_claude_json, agent_data)
    return (
        f"Synced host's {template_id!r} entry into your "
        f"~/.claude.json. Call refresh() so claude respawns and "
        f"loads it."
    )
