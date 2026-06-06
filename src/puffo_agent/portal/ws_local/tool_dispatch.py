"""ws-local ``tool_call`` dispatch.

The existing ``mcp.puffo_core_tools.register_core_tools`` registers
every tool via a ``@mcp.tool()`` decorator that just stashes the
async function on the FastMCP server — the decorator does not
transform the function itself. We exploit that: feed
``register_core_tools`` a stand-in that captures handlers by name,
then expose the subset ws-local clients are allowed to call.

Only the six message-shaped tools are surfaced. Tools that depend on
a harness subprocess (``refresh``, ``reload_system_prompt``) or
operator host config (``install_host_mcp``, ``sync_host_mcp``) make
no sense for an external AI hold the WS — they're filtered here.
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
    "list_channel_members",
})


@dataclass
class _CapturedRegistration:
    """Pretends to be a FastMCP server. ``.tool()`` is the only
    decorator we exercise; everything else is a permissive no-op so
    ``register_core_tools`` doesn't crash if it grows new attributes."""

    handlers: dict[str, WsLocalTool]

    def tool(self, *args: Any, **kwargs: Any):
        def _decorate(fn: WsLocalTool) -> WsLocalTool:
            self.handlers[fn.__name__] = fn
            return fn
        return _decorate

    # FastMCP exposes more registration methods (e.g. ``resource``);
    # stub them out so unrelated future additions don't crash the
    # capture. Each returns a passthrough decorator.
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
    subset, sourced from the real ``puffo_core_tools`` implementations.

    Each call yields a fresh capture so the closures bind the right
    ``cfg`` for that attach session.
    """
    from ...mcp.puffo_core_tools import register_core_tools

    captured = _CapturedRegistration(handlers={})
    register_core_tools(captured, cfg)
    return {
        name: captured.handlers[name]
        for name in allowed
        if name in captured.handlers
    }
