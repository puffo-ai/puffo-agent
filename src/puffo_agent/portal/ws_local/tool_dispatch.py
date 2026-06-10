"""ws-local ``tool_call`` dispatch.

Reuses ``mcp.puffo_core_tools.register_core_tools`` by feeding it a
FastMCP stand-in that captures handlers by name. ws-local exposes
only the six message-shaped tools — subprocess-bound ones
(``refresh``, ``reload_system_prompt``, ``install_host_mcp``,
``sync_host_mcp``) are filtered out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


WsLocalTool = Callable[..., Awaitable[Any]]


WS_LOCAL_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "send_message",
    "send_message_with_attachments",
    "get_user_info",
    "get_post",
    "get_channel_history",
    "get_dm_history",
    "list_channel_members",
})


@dataclass
class _CapturedRegistration:
    """FastMCP stand-in: ``.tool()`` stashes handlers, other
    registration methods (``resource``, ``prompt``) are passthrough
    no-ops so future FastMCP additions don't crash the capture."""

    handlers: dict[str, WsLocalTool]

    def tool(self, *args: Any, **kwargs: Any):
        def _decorate(fn: WsLocalTool) -> WsLocalTool:
            self.handlers[fn.__name__] = fn
            return fn
        return _decorate

    def resource(self, *args: Any, **kwargs: Any):
        def _passthrough(fn):
            return fn
        return _passthrough

    def prompt(self, *args: Any, **kwargs: Any):
        def _passthrough(fn):
            return fn
        return _passthrough


def build_dispatch(
    cfg: Any,
    allowed: frozenset[str] = WS_LOCAL_ALLOWED_TOOLS,
) -> dict[str, WsLocalTool]:
    """Return ``{tool_name: async_handler}`` for the ws-local-allowed
    subset. Each call yields a fresh capture so the closures bind to
    the supplied ``cfg``."""
    from ...mcp.puffo_core_tools import register_core_tools

    captured = _CapturedRegistration(handlers={})
    register_core_tools(captured, cfg)
    return {
        name: captured.handlers[name]
        for name in allowed
        if name in captured.handlers
    }
