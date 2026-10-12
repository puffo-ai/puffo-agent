from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_connector_tools(mcp: FastMCP, cfg: Any) -> None:
    # credentials live in the daemon: no RPC link, no tools
    if getattr(cfg, "rpc_client", None) is None:
        return
    rpc = cfg.rpc_client

    @mcp.tool()
    async def slack_send(
        channel: str, text: str, thread_ts: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Post to Slack as the user your operator connected.

        ``channel`` is a conversation id (``C…``/``G…``/``D…``) from
        ``slack_list_channels`` or a search; ``thread_ts`` replies in a thread.
        ``from_account`` only when several workspaces are shared.

        **"unknown" means it may have been posted: do not post it again**
        without checking the channel. An error means it was not.
        """
        return await rpc.slack_send(
            channel=channel, text=text, thread_ts=thread_ts, from_account=from_account,
        )

    @mcp.tool()
    async def slack_search(
        query: str, limit: int = 10, from_account: str = "",
    ) -> dict[str, Any]:
        """Search Slack messages the connected user can see, newest first.

        ``query`` is Slack search syntax (``in:#general``, ``from:@alice``,
        ``after:2026-10-01``). ``limit`` 1..50.
        """
        return await rpc.connector(
            "slack", "search", query=query, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def slack_read(
        channel: str, limit: int = 20, thread_ts: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """A conversation's latest messages, or with ``thread_ts`` that
        thread's replies. ``limit`` 1..50; long messages are cut and flagged
        ``text_truncated``.
        """
        return await rpc.connector(
            "slack", "read", channel=channel, limit=limit, thread_ts=thread_ts,
            from_account=from_account,
        )

    @mcp.tool()
    async def slack_list_channels(
        types: str = "", limit: int = 100, from_account: str = "",
    ) -> dict[str, Any]:
        """Conversations the connected user can see, with their ids.

        ``types``: comma-separated subset of ``public_channel``,
        ``private_channel``, ``mpim``, ``im``; empty = all. Archived ones are
        left out.
        """
        return await rpc.connector(
            "slack", "list_channels", types=types, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def discord_whoami(from_account: str = "") -> dict[str, Any]:
        """The connected Discord user: id, username, email.

        Discord access is identity only — it cannot read or post messages.
        """
        return await rpc.connector("discord", "whoami", from_account=from_account)

    @mcp.tool()
    async def discord_guilds(from_account: str = "") -> dict[str, Any]:
        """Servers the connected Discord user belongs to, with permission bits."""
        return await rpc.connector("discord", "guilds", from_account=from_account)

    @mcp.tool()
    async def discord_guild_member(
        guild_id: str, from_account: str = "",
    ) -> dict[str, Any]:
        """The connected user's membership in one server (from ``discord_guilds``):
        nickname, role ids, join date."""
        return await rpc.connector(
            "discord", "guild_member", guild_id=guild_id, from_account=from_account,
        )

    @mcp.tool()
    async def discord_connections(from_account: str = "") -> dict[str, Any]:
        """Third-party accounts (GitHub, Steam, …) linked to the Discord user."""
        return await rpc.connector("discord", "connections", from_account=from_account)
