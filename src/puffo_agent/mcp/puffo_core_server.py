"""Standalone MCP stdio server for puffo-core tools.

Combines puffo-core API tools (``puffo_core_tools``) with host-side
/ claude-code-control tools (skills, MCP server mgmt, reload,
refresh) from ``host_tools``.

Entry point: ``python -m puffo_agent.mcp.puffo_core_server``
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore
from .data_client import DataClient
from .host_tools import (
    _install_mcp_server,
    _install_skill,
    _list_mcp_servers,
    _list_skills,
    _uninstall_mcp_server,
    _uninstall_skill,
    _write_refresh_flag,
)
from .puffo_core_tools import PuffoCoreToolsConfig, register_core_tools

logger = logging.getLogger(__name__)


def _register_local_tools(
    mcp: FastMCP,
    workspace: str,
    runtime_kind: str = "",
    harness: str = "",
) -> None:
    """Register system/local tools that don't depend on the messaging API."""

    def _require_claude_code(tool: str) -> None:
        if harness and harness != "claude-code":
            raise RuntimeError(
                f"{tool} is only supported under the claude-code "
                f"harness (this agent is using {harness!r})."
            )

    @mcp.tool()
    async def reload_system_prompt() -> str:
        """Rebuild your system prompt from disk and restart your
        claude subprocess so edits take effect on your next message.
        """
        flag_path = Path(workspace) / ".puffo-agent" / "reload.flag"
        try:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text(
                f'{{"requested_at": {int(time.time())}}}\n',
                encoding="utf-8",
            )
        except OSError as exc:
            raise RuntimeError(f"could not write reload flag: {exc}") from exc
        return (
            "reload requested — your system prompt will be rebuilt and "
            "your claude subprocess restarted before your next message."
        )

    @mcp.tool()
    async def refresh(model: Optional[str] = None) -> str:
        """Respawn your claude subprocess so it re-discovers skills,
        MCP servers, and optionally switches to a new model."""
        _require_claude_code("refresh")
        _write_refresh_flag(Path(workspace), model)
        tail = f" (model override: {model!r})" if model is not None else ""
        return (
            "refresh requested — your claude subprocess will respawn "
            "before your next message" + tail + "."
        )

    @mcp.tool()
    async def install_skill(name: str, content: str) -> str:
        """Install a new skill at project scope."""
        _require_claude_code("install_skill")
        dst = _install_skill(Path(workspace), name, content)
        return (
            f"installed skill {name!r} at project scope ({dst}). "
            "Call refresh() so your next turn picks it up."
        )

    @mcp.tool()
    async def uninstall_skill(name: str) -> str:
        """Remove a skill you previously installed."""
        _require_claude_code("uninstall_skill")
        _uninstall_skill(Path(workspace), name)
        return (
            f"uninstalled skill {name!r}. Call refresh() so your next "
            "turn stops seeing it."
        )

    @mcp.tool()
    async def list_skills() -> str:
        """List every skill available to you, tagged by scope."""
        entries = _list_skills(Path(workspace), Path.home())
        if not entries:
            return "(no skills installed)"
        return "\n".join(
            f"[{scope}]{' ' if scope == 'agent' else ''} {name}"
            for scope, name in entries
        )

    @mcp.tool()
    async def install_mcp_server(
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> str:
        """Register a new stdio MCP server at project scope."""
        _require_claude_code("install_mcp_server")
        check_host_local = runtime_kind != "cli-local"
        path = _install_mcp_server(
            Path(workspace), name, command, args, env,
            check_host_local=check_host_local,
        )
        return (
            f"registered MCP server {name!r} at project scope ({path}). "
            "Call refresh() so the claude subprocess respawns."
        )

    @mcp.tool()
    async def uninstall_mcp_server(name: str) -> str:
        """Remove an MCP server you previously registered."""
        _require_claude_code("uninstall_mcp_server")
        _uninstall_mcp_server(Path(workspace), name)
        return (
            f"removed MCP server {name!r}. Call refresh() so the claude "
            "subprocess respawns without it."
        )

    @mcp.tool()
    async def list_mcp_servers() -> str:
        """List every MCP server available to you, tagged by scope."""
        entries = _list_mcp_servers(Path(workspace), Path.home())
        if not entries:
            return "(no MCP servers registered)"
        return "\n".join(
            f"[{scope}]{' ' if scope == 'agent' else ''} {n}"
            for scope, n in entries
        )


def build_server(
    slug: str,
    device_id: str,
    server_url: str,
    space_id: str,
    keystore_dir: str,
    workspace: str,
    agent_id: str,
    data_service_url: str,
    runtime_kind: str = "",
    harness: str = "",
) -> FastMCP:
    ks = KeyStore(keystore_dir)
    http = PuffoCoreHttpClient(server_url, ks, slug)
    # Read-only client for the daemon's data service. The MCP never
    # opens the agent's SQLite directly — the daemon is the sole
    # reader/writer regardless of where the MCP runs.
    data = DataClient(data_service_url, agent_id)

    core_cfg = PuffoCoreToolsConfig(
        slug=slug,
        device_id=device_id,
        keystore=ks,
        http_client=http,
        data_client=data,
        space_id=space_id,
        workspace=workspace,
    )

    mcp = FastMCP("puffo-core")
    register_core_tools(mcp, core_cfg)
    _register_local_tools(mcp, workspace, runtime_kind, harness)
    return mcp


def _cfg_from_env() -> dict[str, str]:
    required = {
        "slug": os.environ.get("PUFFO_CORE_SLUG", ""),
        "device_id": os.environ.get("PUFFO_CORE_DEVICE_ID", ""),
        "server_url": os.environ.get("PUFFO_CORE_SERVER_URL", ""),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            f"missing required env vars: {', '.join('PUFFO_CORE_' + k.upper() for k in missing)}"
        )
    # ``PUFFO_AGENT_ID`` falls back to the slug — agent_id and the
    # disambiguated slug are equal by construction.
    slug_value = required["slug"]
    agent_id = os.environ.get("PUFFO_AGENT_ID", slug_value)
    # Default targets the daemon's loopback data service. cli-docker
    # overrides this with ``http://host.docker.internal:63386``.
    data_service_url = os.environ.get(
        "PUFFO_DATA_SERVICE_URL", "http://127.0.0.1:63386",
    )
    return {
        **required,
        "space_id": os.environ.get("PUFFO_CORE_SPACE_ID", ""),
        "keystore_dir": os.environ.get("PUFFO_CORE_KEYSTORE_DIR", ""),
        "workspace": os.environ.get("PUFFO_WORKSPACE", "/workspace"),
        "agent_id": agent_id,
        "data_service_url": data_service_url,
        "runtime_kind": os.environ.get("PUFFO_RUNTIME_KIND", ""),
        "harness": os.environ.get("PUFFO_HARNESS", ""),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = _cfg_from_env()
    server = build_server(**cfg)
    server.run()


if __name__ == "__main__":
    main()
