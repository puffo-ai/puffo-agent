"""Conversation discovery and history MCP tool registration."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_history_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register history and directory tools in their established order."""
    from .core_directory_tools import register_directory_tools
    from .core_post_tools import register_post_tools

    register_directory_tools(mcp, cfg)
    register_post_tools(mcp, cfg)
