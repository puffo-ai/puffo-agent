"""Outbound message MCP tool registration."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.send_coordinator import SemanticSendRequest
from .puffo_core_tools import _dispatch_semantic_send
from .core_inbox_tools import resolve_inbox_runtime
from .tool_result_projection import (
    ToolResultSurface,
    project_send_result,
)


def register_message_tools(
    mcp: FastMCP,
    cfg: Any,
    *,
    result_surface: ToolResultSurface = "stdio_mcp",
) -> None:
    @mcp.tool(structured_output=False)
    async def send_message(
        channel: str,
        text: str,
        root_id: str = "",
        visibility_level: str = "default",
        send_anyway: bool = False,
        covers: list[str] | None = None,
    ) -> Any:
        """Post text to a channel ``ch_<uuid>`` or DM ``@<slug>``.

        ``root_id`` is an optional thread root; ``visibility_level`` is
        ``human``, ``default`` (default), or ``agent_only``; ``send_anyway``
        is an explicit held-send flag. ``covers`` lists the inbound
        message ids this send disposes of — declare them on every reply;
        messages left uncovered at turn end may be redelivered once as
        uncovered. Results are ``sent``, ``held``, or an error. See the
        managed ``send-message`` skill for routing, visibility, thread-root
        validation, and held-send guidance.
        """
        return project_send_result(
            await _dispatch_semantic_send(
                cfg,
                SemanticSendRequest(
                    destination=channel,
                    text=text,
                    root_id=root_id,
                    visibility_level=visibility_level,
                    send_anyway=send_anyway,
                    covers=tuple(covers or ()),
                ),
            ),
            surface=result_surface,
        )

    @mcp.tool(structured_output=False)
    async def send_message_with_attachments(
        paths: list[str],
        channel: str,
        caption: str = "",
        root_id: str = "",
        visibility_level: str = "default",
        send_anyway: bool = False,
        covers: list[str] | None = None,
    ) -> Any:
        """Send workspace files and an optional caption to a channel or DM.

        ``paths`` are workspace-relative; ``channel``, ``root_id``,
        ``visibility_level``, ``send_anyway``, and ``covers`` match
        ``send_message``. Results are ``sent``, ``held``, or an error. See
        the managed ``send-message-with-attachments`` skill and its common
        ``send-message`` held-send procedure.
        """
        return project_send_result(
            await _dispatch_semantic_send(
                cfg,
                SemanticSendRequest(
                    destination=channel,
                    attachment_paths=(tuple(paths) if isinstance(paths, list) else ()),
                    caption=caption,
                    root_id=root_id,
                    visibility_level=visibility_level,
                    send_anyway=send_anyway,
                    covers=tuple(covers or ()),
                ),
                tool_name="send_message_with_attachments",
            ),
            surface=result_surface,
        )

    _register_mark_covered(mcp, cfg)


def _register_mark_covered(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def mark_covered(
        covers: list[str],
        by_message_id: str = "",
        note: str = "",
    ) -> Any:
        """Mark inbound messages as disposed without sending anything.

        Use this for a message that needs no reply (pass a short ``note``
        saying why), or to backfill covers a previous send forgot
        (``by_message_id`` names that sent message). Unknown ids come back
        as an explicit error listing exactly which ids failed.
        """
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            return await runtime.mark_covered(
                covers=covers,
                by_message_id=by_message_id,
                note=note,
            )
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.mark_covered(
                covers=covers,
                by_message_id=by_message_id,
                note=note,
            )
        raise RuntimeError("global Inbox runtime is unavailable")
