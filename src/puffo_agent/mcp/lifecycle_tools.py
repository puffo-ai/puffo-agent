"""Bridge-only sandbox-lifecycle MCP tools (T23).

Registered *only* for cloud (bridge-transport) agents — gated on
``cfg.bridge_client is not None`` at the ``register_core_tools`` call
site — and exposed over the ws-local dispatch allowlist
(``WS_LOCAL_ALLOWED_TOOLS``). A native/desktop agent (signed-crypto
transport, ``bridge_client is None``) never registers them, so this
whole surface is invisible to native agents.

Every tool is a thin, fail-soft wrapper over the ``CloudBridgeClient``
keyless ``x-sandbox-token`` lifecycle methods: a failing lifecycle call
returns a plain error string the model can reason about, never an
exception that would crash the turn. The server owns all wake / sleep
state — these tools are stateless request/response calls, no
persistence or background scheduling on the agent side.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)


def register_lifecycle_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register cloud-lifecycle tools in their established public order."""
    from .lifecycle_tool_registration import (
        register_cancel_wake_tool,
        register_get_runtime_status_tool,
        register_get_scheduled_wake_tool,
        register_keep_alive_tool,
        register_schedule_wake_tool,
    )

    register_schedule_wake_tool(mcp, cfg)
    register_cancel_wake_tool(mcp, cfg)
    register_get_scheduled_wake_tool(mcp, cfg)
    register_get_runtime_status_tool(mcp, cfg)
    register_keep_alive_tool(mcp, cfg)
