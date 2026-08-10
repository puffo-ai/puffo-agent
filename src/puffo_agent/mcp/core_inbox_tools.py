"""Inbox and durable reminder MCP tool registration."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.context_controller import MODEL_VISIBLE_READ_RECEIPT_PREFIX


def register_inbox_read_tool(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def read_inbox(
        target: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read one pending Inbox page.

        target is an optional canonical target; cursor is the opaque next
        cursor; limit is 1..50. Results contain ``messages``, bounded
        read-only ``prior_context``, ``prior_context_has_more``,
        ``next_cursor``, and ``has_more``.
        See the managed ``read-inbox`` skill for interpretation and paging.
        """
        arguments: dict[str, Any] = {}
        if target:
            arguments["target"] = target
        if cursor:
            arguments["cursor"] = cursor
        if limit != 50:
            arguments["limit"] = limit
        runtime = getattr(cfg, "inbox_runtime", None)
        if runtime is None:
            runtime = getattr(
                getattr(cfg, "message_client", None), "global_runtime", None
            )
        if runtime is not None:
            result = await runtime.read_inbox(
                target=target,
                cursor=cursor,
                limit=limit,
                tool_arguments=arguments,
            )
        elif cfg.rpc_client is not None:
            result = await cfg.rpc_client.read_inbox(
                target=target, cursor=cursor, limit=limit
            )
        else:
            raise RuntimeError("global Inbox runtime is unavailable")
        receipt = result.pop("correlation_receipt", "")
        if receipt:
            result["admission_receipt"] = (
                f"[{MODEL_VISIBLE_READ_RECEIPT_PREFIX}{receipt}]"
            )
        return result


def register_reminder_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def create_reminder(
        content: str,
        target: str,
        intended_at: str,
    ) -> dict[str, Any]:
        """Create one durable local reminder for a canonical Inbox target.

        ``intended_at`` is an explicit-offset RFC3339 timestamp. The result
        is a provider-neutral reminder object. Reminders are immutable and
        single-fire; replace a pending reminder by cancelling it and creating
        another. A due occurrence enters the ordinary durable Inbox and leaves
        any action decision to the model.
        """
        runtime = getattr(cfg, "inbox_runtime", None)
        if runtime is None:
            runtime = getattr(
                getattr(cfg, "message_client", None),
                "global_runtime",
                None,
            )
        if runtime is not None:
            return await runtime.create_reminder(
                content=content,
                target=target,
                intended_at=intended_at,
            )
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.create_reminder(
                content=content,
                target=target,
                intended_at=intended_at,
            )
        raise RuntimeError("global Inbox runtime is unavailable")

    @mcp.tool()
    async def list_reminders(
        state: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List durable reminders, including target, content, time, and state."""
        runtime = getattr(cfg, "inbox_runtime", None)
        if runtime is None:
            runtime = getattr(
                getattr(cfg, "message_client", None),
                "global_runtime",
                None,
            )
        if runtime is not None:
            return await runtime.list_reminders(state=state, limit=limit)
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.list_reminders(state=state, limit=limit)
        raise RuntimeError("global Inbox runtime is unavailable")

    @mcp.tool()
    async def cancel_reminder(reminder_id: str) -> dict[str, Any]:
        """Idempotently cancel a local reminder that has not delivered."""
        runtime = getattr(cfg, "inbox_runtime", None)
        if runtime is None:
            runtime = getattr(
                getattr(cfg, "message_client", None),
                "global_runtime",
                None,
            )
        if runtime is not None:
            return await runtime.cancel_reminder(reminder_id=reminder_id)
        if cfg.rpc_client is not None:
            return await cfg.rpc_client.cancel_reminder(reminder_id=reminder_id)
        raise RuntimeError("global Inbox runtime is unavailable")


def register_inbox_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register Inbox and reminder tools in their established order."""
    register_inbox_read_tool(mcp, cfg)
    register_reminder_tools(mcp, cfg)
