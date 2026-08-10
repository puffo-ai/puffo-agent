"""MCP registration for membership tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .puffo_core_tools import _resolve_channel_space


def register_membership_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def leave_space(space_id: str, reason: str = "") -> str:
        """Request to leave a space. This does NOT leave immediately —
        it asks your operator to approve. The operator gets a DM and
        replies `y` (you leave) or `n` (you stay); you'll see their
        decision in that thread.

        space_id: the space to leave (e.g. 'sp_<uuid>'). Use
            ``list_spaces`` to find it.
        reason: optional — a short why, shown to the operator in the
            approval DM. Be honest and specific.

        Note: a space owner can't leave directly, and the request only
        goes through while you're a member.
        """
        sp = space_id.strip()
        if not sp:
            raise RuntimeError("space_id is required")
        # ws-local runs in-process → drive the client directly; harness
        # runtimes go through the daemon's rpc_service.
        if cfg.message_client is not None:
            return await cfg.message_client.request_leave_approval(
                kind="leave_space",
                space_id=sp,
                channel_id="",
                reason=reason,
            )
        if cfg.rpc_client is None:
            raise RuntimeError(
                "leave_space unavailable — PUFFO_RPC_URL not set on this "
                "MCP runtime, so the puffo-agent daemon isn't reachable."
            )
        return await cfg.rpc_client.request_leave(
            kind="leave_space",
            space_id=sp,
            channel_id="",
            reason=reason,
        )

    @mcp.tool()
    async def leave_channel(channel_id: str, reason: str = "") -> str:
        """Request to leave a channel. This does NOT leave immediately —
        it asks your operator to approve. The operator gets a DM and
        replies `y` (you leave) or `n` (you stay); you'll see their
        decision in that thread.

        channel_id: the channel to leave (e.g. 'ch_<uuid>'). Use
            ``list_channels_in_all_spaces`` to find it.
        reason: optional — a short why, shown to the operator in the
            approval DM.

        Note: public channels can't be left on their own — to leave one
        you'd leave the whole space (``leave_space``).
        """
        ch = channel_id.strip()
        if not ch:
            raise RuntimeError("channel_id is required")
        space_id = await _resolve_channel_space(cfg, ch)
        if cfg.message_client is not None:
            return await cfg.message_client.request_leave_approval(
                kind="leave_channel",
                space_id=space_id,
                channel_id=ch,
                reason=reason,
            )
        if cfg.rpc_client is None:
            raise RuntimeError(
                "leave_channel unavailable — PUFFO_RPC_URL not set on "
                "this MCP runtime, so the puffo-agent daemon isn't "
                "reachable."
            )
        return await cfg.rpc_client.request_leave(
            kind="leave_channel",
            space_id=space_id,
            channel_id=ch,
            reason=reason,
        )
