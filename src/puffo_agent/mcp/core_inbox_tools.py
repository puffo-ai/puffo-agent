"""Inbox and durable reminder MCP tool registration."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import format_inbox_read_result
from ..agent.message_store_models import parse_inbox_target
from .core_history_read_tools import (
    history_tool_arguments,
    read_history_messages,
)
from .tool_result_projection import (
    ToolResultSurface,
    project_text_result,
)


def _normalize_inbox_read_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize the pre-window RPC shape during rolling local upgrades."""
    return {
        "context_version": int(result.get("context_version", 1)),
        "messages": result["messages"],
        "next_cursor": result["next_cursor"],
        "has_more": result["has_more"],
        "remaining_count": result["remaining_count"],
        "snapshot_generation": result["snapshot_generation"],
    }


def _inbox_tool_arguments(*, target: str, cursor: str, limit: int) -> dict[str, object]:
    arguments: dict[str, object] = {}
    if target:
        arguments["target"] = target
    if cursor:
        arguments["cursor"] = cursor
    if limit != 50:
        arguments["limit"] = limit
    return arguments


def resolve_inbox_runtime(cfg):
    """The in-process runtime when wired, else None (callers fall back to RPC)."""
    runtime = getattr(cfg, "inbox_runtime", None)
    if runtime is None:
        runtime = getattr(
            getattr(cfg, "message_client", None), "global_runtime", None
        )
    return runtime


def register_message_read_tools(
    mcp: FastMCP,
    cfg: Any,
    *,
    result_surface: ToolResultSurface = "stdio_mcp",
) -> None:
    @mcp.tool(structured_output=False)
    async def read_inbox(
        target: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> Any:
        """Read messages that are still pending in this agent's Inbox.

        ``target`` optionally scopes one DM/channel/thread. ``cursor``
        continues the exact pending snapshot returned by the previous page.
        Returned pending messages enter the current model turn automatically.
        See the managed ``read-messages`` skill for paging and boundaries.
        """
        if isinstance(limit, bool):
            raise RuntimeError("limit must be an integer")
        if not 1 <= int(limit) <= 50:
            raise RuntimeError("limit must be between 1 and 50")
        if target:
            try:
                parse_inbox_target(target)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            result = await runtime.read_inbox(
                target=target,
                cursor=cursor,
                limit=limit,
                tool_arguments=_inbox_tool_arguments(
                    target=target, cursor=cursor, limit=limit,
                ),
            )
        elif cfg.rpc_client is not None:
            result = await cfg.rpc_client.read_inbox(
                target=target, cursor=cursor, limit=limit
            )
        else:
            raise RuntimeError("global Inbox runtime is unavailable")
        return project_text_result(
            format_inbox_read_result(_normalize_inbox_read_result(result)),
            surface=result_surface,
        )

    @mcp.tool(structured_output=False)
    async def read_history(
        target: str = "",
        cursor: str = "",
        before_message_id: str = "",
        after_message_id: str = "",
        limit: int = 50,
    ) -> Any:
        """Read earlier conversation context for one DM/channel/thread.

        Start with ``target`` and optionally one exclusive message-id boundary.
        Continue a returned older/newer page with ``cursor``. Results use the
        same semantic message grammar as ``read_inbox``. See the managed
        ``read-messages`` skill for paging and local-history boundaries.
        """
        arguments = history_tool_arguments(
            target=target,
            cursor=cursor,
            before_message_id=before_message_id,
            after_message_id=after_message_id,
            limit=limit,
        )
        return project_text_result(
            await read_history_messages(
                cfg,
                target=target,
                cursor=cursor,
                before_message_id=before_message_id,
                after_message_id=after_message_id,
                limit=limit,
                tool_arguments=arguments,
            ),
            surface=result_surface,
        )


def register_reminder_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def create_reminder(
        content: str,
        target: str,
        intended_at: str,
        covers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create one durable local reminder for a canonical Inbox target.

        ``intended_at`` is an explicit-offset RFC3339 timestamp. ``covers``
        lists inbound message ids this deferral disposes of, exactly like
        ``send_message`` covers. The result is a provider-neutral reminder
        object. A due occurrence enters the ordinary durable Inbox and
        leaves any action decision to the model.
        """
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            return await runtime.create_reminder(
                content=content,
                target=target,
                intended_at=intended_at,
                covers=covers,
            )
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.create_reminder(
                content=content,
                target=target,
                intended_at=intended_at,
                covers=covers,
            )
        raise RuntimeError("global Inbox runtime is unavailable")

    @mcp.tool()
    async def list_reminders(
        state: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List durable reminders, including target, content, time, and state."""
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            return await runtime.list_reminders(state=state, limit=limit)
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.list_reminders(state=state, limit=limit)
        raise RuntimeError("global Inbox runtime is unavailable")

    @mcp.tool()
    async def cancel_reminder(reminder_id: str) -> dict[str, Any]:
        """Idempotently cancel a reminder whose delivery has not started."""
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            return await runtime.cancel_reminder(reminder_id=reminder_id)
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.cancel_reminder(reminder_id=reminder_id)
        raise RuntimeError("global Inbox runtime is unavailable")


def register_reminder_replace_tool(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def replace_reminder(
        reminder_id: str,
        content: str = "",
        target: str = "",
        intended_at: str = "",
    ) -> dict[str, Any]:
        """Atomically replace one scheduled reminder.

        Supply one or more changed fields; empty fields inherit the existing
        value. ``intended_at`` uses explicit-offset RFC3339. The result contains
        the cancelled reminder and its scheduled replacement. Delivery that
        already started is never revoked.
        """
        runtime = resolve_inbox_runtime(cfg)
        if runtime is not None:
            return await runtime.replace_reminder(
                reminder_id=reminder_id,
                content=content,
                target=target,
                intended_at=intended_at,
            )
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.replace_reminder(
                reminder_id=reminder_id,
                content=content,
                target=target,
                intended_at=intended_at,
            )
        raise RuntimeError("global Inbox runtime is unavailable")


def register_inbox_tools(
    mcp: FastMCP,
    cfg: Any,
    *,
    result_surface: ToolResultSurface = "stdio_mcp",
) -> None:
    """Register Inbox and reminder tools in their established order."""
    register_message_read_tools(mcp, cfg, result_surface=result_surface)
    register_reminder_tools(mcp, cfg)
    register_reminder_replace_tool(mcp, cfg)
