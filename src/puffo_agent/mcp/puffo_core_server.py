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
from ._lifespan import make_lifespan
from .data_client import DataClient
from ._host_mcp import PuffoRpcClient
from .host_tools import (
    _install_mcp_server,
    _install_skill,
    _list_mcp_servers,
    _list_skills,
    _touch_refresh_flag,
    _uninstall_mcp_server,
    _uninstall_skill,
    _write_refresh_model_flag,
)
from .puffo_core_tools import PuffoCoreToolsConfig, register_core_tools

logger = logging.getLogger(__name__)


_REFRESH_HARNESSES: tuple[str, ...] = ("claude-code", "codex")


def _validate_refresh_model(harness: str, model: Optional[str]) -> None:
    if harness not in _REFRESH_HARNESSES:
        raise RuntimeError(
            f"harness={harness!r} not supported by refresh; "
            f"choose one of: {list(_REFRESH_HARNESSES)}"
        )
    from ..agent.cli_bin import resolve_claude_bin, resolve_codex_bin
    resolver = {
        "claude-code": resolve_claude_bin,
        "codex": resolve_codex_bin,
    }[harness]
    if resolver() is None:
        raise RuntimeError(
            f"harness={harness!r} not installed on host — the CLI "
            "binary is missing from PATH."
        )
    from ..agent.model_catalog import provider_models
    supported = [m.id for m in provider_models(harness) if m.id]
    if (model or "") not in supported:
        raise RuntimeError(
            f"model={model!r} not supported by harness={harness!r}; "
            f"supported: {supported}"
        )


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
    async def refresh(
        harness: Optional[str] = None,
        model: Optional[str] = None,
        host_sync: bool = False,
        session: bool = False,
    ) -> str:
        """Refresh your agent state. Four orthogonal axes:

        * no args — rebuild CLAUDE.md + re-sync puffo default skills.
        * ``host_sync=True`` — also re-sync operator's host skills + MCP.
        * ``session=True`` — drop CLI session so next spawn is fresh.
        * ``harness`` + ``model`` (both required together) — swap
          harness/model, persist to agent.yml, full worker respawn.

        Requires ``cli-local`` / ``cli-docker`` runtime. On cli-docker,
        ``host_sync=True`` requires ``session=True`` (or harness+model).
        """
        if runtime_kind and runtime_kind not in ("cli-local", "cli-docker"):
            raise RuntimeError(
                f"refresh requires cli-local or cli-docker; this agent "
                f"is running under kind={runtime_kind!r}."
            )
        if (harness is None) != (model is None):
            raise RuntimeError(
                "harness and model must be provided together (or both "
                "omitted)."
            )
        if harness is not None:
            _validate_refresh_model(harness, model)
        if host_sync and runtime_kind == "cli-docker" and not session and harness is None:
            raise RuntimeError(
                "refresh(host_sync=True) on cli-docker requires "
                "session=True (the container has to restart to pick "
                "up new host skills/MCP)."
            )
        ws = Path(workspace)
        touched: list[str] = []
        if harness is not None:
            _write_refresh_model_flag(ws, harness=harness, model=model or "")
            touched.append(f"refresh_model (harness={harness!r} model={model!r})")
        else:
            _touch_refresh_flag(ws, "refresh_agent")
            touched.append("refresh_agent")
            if host_sync:
                _touch_refresh_flag(ws, "refresh_host_sync")
                touched.append("refresh_host_sync")
            if session:
                _touch_refresh_flag(ws, "refresh_session")
                touched.append("refresh_session")
        return "refresh requested: " + ", ".join(touched)

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
        """List every MCP server available to you, tagged by scope.

        Scopes:
          * ``system`` — installed via ``claude mcp add`` on the host.
          * ``agent``  — project-scope, installed by this agent via
            ``install_mcp_server``.
          * ``plugin`` — provided by a ``claude /plugin install``-ed
            plugin; trailing ``(from <plugin>/<version>)`` cites the
            owning plugin so the operator can tell which plugin
            registered the server (and ``claude /plugin uninstall``
            the right one if needed).
        """
        entries = _list_mcp_servers(Path(workspace), Path.home(), harness)
        if not entries:
            return "(no MCP servers registered)"
        lines: list[str] = []
        for scope, name, source in entries:
            # Pad scope tag so columns line up across rows — every
            # scope fits in 6 chars, ``[plugin]`` is the widest.
            tag = f"[{scope:<6}]"
            if source:
                lines.append(f"{tag} {name}  (from {source})")
            else:
                lines.append(f"{tag} {name}")
        return "\n".join(lines)


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
    data = DataClient(data_service_url, agent_id)

    # None when PUFFO_RPC_URL is unset; tools surface a clear error
    # instead of crashing the whole MCP at startup.
    rpc_url = os.environ.get("PUFFO_RPC_URL", "")
    rpc_client = (
        PuffoRpcClient(rpc_url, agent_id) if rpc_url else None
    )

    core_cfg = PuffoCoreToolsConfig(
        slug=slug,
        device_id=device_id,
        keystore=ks,
        http_client=http,
        data_client=data,
        space_id=space_id,
        workspace=workspace,
        rpc_client=rpc_client,
    )

    # Lifespan closes adapter sessions while the loop is alive,
    # silencing the ``Unclosed client session`` gc warning.
    mcp = FastMCP(
        "puffo-core",
        lifespan=make_lifespan(data, rpc_client, http),
    )
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
