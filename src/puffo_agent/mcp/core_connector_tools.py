from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_connector_tools(mcp: FastMCP, cfg: Any) -> None:
    """Slack and Discord, as the account the operator connected in
    Settings → Apps. The daemon holds the credentials; without an RPC link
    there is nothing to call with."""
    if getattr(cfg, "rpc_client", None) is None:
        return
    rpc = cfg.rpc_client

    @mcp.tool()
    async def slack_send(
        channel: str, text: str, thread_ts: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Post a message to Slack as the user your operator connected.

        ``channel`` is a conversation id (``C…``, ``G…``, ``D…``) from
        ``slack_list_channels`` or a search result; ``thread_ts`` replies in
        that thread. ``from_account`` is only needed when several workspaces
        are shared; the error lists the names to choose from.

        **"unknown" means it may have been posted: do not post it again** —
        check the channel first. An error means it was not.
        """
        return await rpc.slack_send(
            channel=channel, text=text, thread_ts=thread_ts, from_account=from_account,
        )

    @mcp.tool()
    async def slack_search(
        query: str, limit: int = 10, from_account: str = "",
    ) -> dict[str, Any]:
        """Find Slack messages the connected user can see, newest first.

        ``query`` is Slack search syntax — ``in:#general``, ``from:@alice``,
        ``after:2026-10-01``, quoted phrases — and combinations. ``limit`` is
        1..50. Each result carries ``channel`` and ``ts`` for ``slack_read``
        and ``slack_send``.
        """
        return await rpc.connector(
            "slack", "search", query=query, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def slack_read(
        channel: str, limit: int = 20, thread_ts: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Read a conversation's latest messages (newest first), or with
        ``thread_ts`` one thread's replies (oldest first). ``limit`` is 1..50.
        Authors come with a display name when Slack will say; long messages
        are cut and ``text_truncated`` says so.
        """
        return await rpc.connector(
            "slack", "read", channel=channel, limit=limit, thread_ts=thread_ts,
            from_account=from_account,
        )

    @mcp.tool()
    async def slack_list_channels(
        types: str = "", limit: int = 100, from_account: str = "",
    ) -> dict[str, Any]:
        """List the conversations the connected user can see, with their ids.

        ``types`` is a comma-separated subset of ``public_channel``,
        ``private_channel``, ``mpim``, ``im``; empty means all four. A DM
        (``im``) is named after the other person. Archived ones are left out.
        """
        return await rpc.connector(
            "slack", "list_channels", types=types, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def discord_whoami(from_account: str = "") -> dict[str, Any]:
        """The Discord user your operator connected: id, username, email.

        Discord access is identity only — the grant cannot read or post
        messages. ``from_account`` is only needed when several accounts are
        shared.
        """
        return await rpc.connector("discord", "whoami", from_account=from_account)

    @mcp.tool()
    async def discord_guilds(from_account: str = "") -> dict[str, Any]:
        """The servers (guilds) the connected Discord user belongs to, with
        the user's permission bits in each."""
        return await rpc.connector("discord", "guilds", from_account=from_account)

    @mcp.tool()
    async def discord_guild_member(
        guild_id: str, from_account: str = "",
    ) -> dict[str, Any]:
        """The connected user's membership in one server: nickname, role ids,
        join date. ``guild_id`` comes from ``discord_guilds``."""
        return await rpc.connector(
            "discord", "guild_member", guild_id=guild_id, from_account=from_account,
        )

    @mcp.tool()
    async def discord_connections(from_account: str = "") -> dict[str, Any]:
        """The third-party accounts (GitHub, Steam, …) linked to the connected
        Discord user, as Discord shows them."""
        return await rpc.connector("discord", "connections", from_account=from_account)
