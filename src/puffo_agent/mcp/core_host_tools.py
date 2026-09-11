"""Host integration, membership, and DM preference MCP tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_host_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register host, membership, and DM preference tools in order."""
    from .core_dm_preference_tools import register_dm_preference_tools
    from .core_host_mcp_tools import register_host_mcp_tools
    from .core_membership_tools import register_membership_tools

    register_host_mcp_tools(mcp, cfg)
    register_membership_tools(mcp, cfg)
    register_dm_preference_tools(mcp, cfg)
