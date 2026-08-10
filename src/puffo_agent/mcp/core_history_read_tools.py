"""MCP registration for history read tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import (
    format_message_group,
)
from .data_client import DataNotFound
from .puffo_core_tools import (
    _resolve_channel_space,
    _stage_model_visible_messages,
)


def register_channel_and_dm_history_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_channel_history(
        channel: str,
        limit: int = 20,
        since: str = "",
        before: int = 0,
        after: int = 0,
    ) -> str:
        """List local channel root posts.

        ``channel`` is ``ch_<uuid>``; ``limit``, ``since``, ``before``, and
        ``after`` filter the result. It returns semantic target/row projection
        groups with root reply counts; use ``get_thread_history`` for replies.
        See the managed ``channel-history`` skill for context use.
        """
        limit = max(1, min(int(limit), 200))
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        # Local-store read would return empty on a slug — route non-
        # ``ch_`` refs through the resolver purely for its hint error.
        if not channel_ref.startswith("ch_"):
            await _resolve_channel_space(cfg, channel_ref)
        channel_id = channel_ref

        try:
            roots = await cfg.data_client.get_channel_roots(
                channel_id,
                limit=limit,
                since_envelope_id=since or None,
                before_ts=int(before) if before else None,
                after_ts=int(after) if after else None,
            )
        except DataNotFound:
            return f"(no such channel: {channel_id})"
        if not roots:
            return "(no root posts in the requested window)"
        tool_arguments: dict[str, object] = {"channel": channel}
        if limit != 20:
            tool_arguments["limit"] = limit
        if since:
            tool_arguments["since"] = since
        if before:
            tool_arguments["before"] = before
        if after:
            tool_arguments["after"] = after
        receipt_marker = await _stage_model_visible_messages(
            cfg,
            [entry.message for entry in roots],
            tool_name="get_channel_history",
            tool_arguments=tool_arguments,
        )
        result = format_message_group(
            [entry.message for entry in roots],
            current_agent_aliases=(cfg.slug,),
            reply_counts={
                entry.message.envelope_id: entry.reply_count for entry in roots
            },
        )
        return f"{result}\n{receipt_marker}" if receipt_marker else result

    @mcp.tool()
    async def get_dm_history(
        peer: str,
        limit: int = 20,
        before: int = 0,
    ) -> str:
        """List local DMs with ``peer``, oldest first.

        ``before`` is an exclusive ms-epoch paging bound. Results use the
        semantic target/row projection. See the managed ``channel-history``
        skill for supplementary-context guidance.
        """
        limit = max(1, min(int(limit), 200))
        peer_slug = peer.strip().lstrip("@")
        if not peer_slug:
            raise RuntimeError("pass the peer's slug to read DM history.")
        msgs = await cfg.data_client.get_dm_history(
            peer_slug,
            limit=limit,
            before=int(before) if before else None,
        )
        if not msgs:
            return "(no direct messages with that peer in the requested window)"
        return format_message_group(msgs, current_agent_aliases=(cfg.slug,))


def register_thread_history_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_thread_history(
        root_id: str,
        limit: int = 50,
        since: str = "",
        before: int = 0,
        after: int = 0,
    ) -> str:
        """List a local thread's root and replies, oldest first.

        ``root_id`` is the root envelope id; ``limit``, ``since``, ``before``,
        and ``after`` filter it. Results use the semantic target/row projection.
        See the managed ``channel-history`` skill for supplementary context.
        """
        if not root_id.strip():
            raise RuntimeError("root_id required")
        limit = max(1, min(int(limit), 200))
        try:
            msgs = await cfg.data_client.get_thread_messages(
                root_id.strip(),
                limit=limit,
                since_envelope_id=since or None,
                before_ts=int(before) if before else None,
                after_ts=int(after) if after else None,
            )
        except DataNotFound:
            return f"(no such thread: {root_id.strip()})"
        if not msgs:
            return "(no messages in this thread for the requested window)"
        tool_arguments = {"root_id": root_id}
        if limit != 50:
            tool_arguments["limit"] = limit
        if since:
            tool_arguments["since"] = since
        if before:
            tool_arguments["before"] = before
        if after:
            tool_arguments["after"] = after
        receipt_marker = await _stage_model_visible_messages(
            cfg,
            msgs,
            tool_name="get_thread_history",
            tool_arguments=tool_arguments,
        )
        result = format_message_group(
            msgs,
            current_agent_aliases=(cfg.slug,),
            thread_root_id=root_id.strip(),
        )
        return f"{result}\n{receipt_marker}" if receipt_marker else result


def register_history_read_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register conversation history tools in their established order."""
    register_channel_and_dm_history_tools(mcp, cfg)
    register_thread_history_tools(mcp, cfg)
