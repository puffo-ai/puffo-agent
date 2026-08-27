"""MCP registration for host mcp tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP


def register_host_mcp_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def install_host_mcp(
        name: str,
        spec: Optional[dict] = None,
        template_id: str = "",
    ) -> str:
        """Lay down an MCP server spec into the operator's host
        ``~/.claude.json`` so they can complete OAuth / paste API keys
        on their own claude session, then auto-DM them a one-line
        install confirmation. Pair with ``sync_host_mcp`` once they
        confirm. If you have setup-context to share (docs URL, env
        keys to populate, gotchas) send a separate follow-up message
        — the auto-DM is intentionally minimal.

        ``name``: the key the entry registers under
            (``mcpServers[<name>]`` on host).

        Pass exactly ONE of the two source forms:

        - ``template_id``: look up the spec from puffo-server's
          ``/v2/mcp-templates/<id>`` catalog. Use when the MCP is
          operator-curated and ``desired_mcp`` ships an empty-env
          placeholder you need credentials for.
        - ``spec``: pass an inline MCP server config dict transcribed
          from the MCP package's own README — useful when you find
          an MCP on the web (e.g. Coinbase CDP MCP) that isn't in
          puffo-server's catalog. Shape:
            ``{"type": "stdio", "command": "npx", "args": [...], "env": {...}}``
            ``{"type": "http"|"sse", "url": "https://...", "env": {...}}``
          Set ``env`` values to empty strings for placeholders the
          operator needs to populate.

        Behaviour:
          - host already has the entry → file untouched, no DM, tells
            you to skip to ``sync_host_mcp``.
          - catalog / spec validation / file write fails → tool errors,
            no side effects.
          - host write succeeds + DM succeeds → returns the DM's
            envelope_id; wait for the operator's ping.
          - host write succeeds + DM fails → returns the prebuilt body
            so you can retry via ``send_message`` yourself.
        """
        if cfg.rpc_client is None:
            raise RuntimeError(
                "install_host_mcp unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        return await cfg.rpc_client.install_mcp(
            name=name,
            template_id=template_id,
            spec=spec,
        )

    @mcp.tool()
    async def sync_host_mcp(template_id: str) -> str:
        """Copy the operator's ``~/.claude.json#mcpServers[<id>]``
        entry into your own ``<agent>/.claude.json``. Pair with
        ``install_host_mcp`` once the operator finishes OAuth on host,
        then the runtime automatically reloads the provider at the next
        idle boundary so it picks up the new MCP.

        If the host config doesn't have the entry yet, returns an
        error asking you to call ``install_host_mcp`` first (and
        relay the result to the operator).
        """
        if cfg.rpc_client is None:
            raise RuntimeError(
                "sync_host_mcp unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        result = await cfg.rpc_client.sync_mcp(template_id=template_id)
        workspace = getattr(cfg, "workspace", None)
        synced = result.startswith(("Verified host's ", "Synced host's "))
        if workspace and synced:
            from .host_tools import _touch_refresh_flag

            _touch_refresh_flag(Path(workspace), "refresh_agent")
            result += " Runtime refresh requested."
        return result
