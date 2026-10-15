from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_connector_tools(mcp: FastMCP, cfg: Any) -> None:
    # credentials live in the daemon: no RPC link, no tools
    if getattr(cfg, "rpc_client", None) is None:
        return
    rpc = cfg.rpc_client
    _register_slack(mcp, rpc)
    _register_discord(mcp, rpc)
    _register_notion(mcp, rpc)
    _register_calendar(mcp, rpc)


def _register_slack(mcp: FastMCP, rpc: Any) -> None:
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


def _register_discord(mcp: FastMCP, rpc: Any) -> None:
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


def _register_notion(mcp: FastMCP, rpc: Any) -> None:
    @mcp.tool()
    async def notion_search(
        query: str = "", limit: int = 10, page_token: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Search Notion pages and databases the connected integration can see.

        Empty ``query`` lists everything shared with the integration. ``limit``
        1..100; ``page_token`` is a previous call's ``next_page_token``.
        """
        return await rpc.connector(
            "notion", "search", query=query, limit=limit, page_token=page_token,
            from_account=from_account,
        )

    @mcp.tool()
    async def notion_read_page(page_id: str, from_account: str = "") -> dict[str, Any]:
        """A page's title, property summary, and text content (one level of blocks).

        ``page_id`` is from ``notion_search``. Long content is cut and flagged
        ``content_truncated``.
        """
        return await rpc.connector("notion", "read_page", page_id=page_id, from_account=from_account)

    @mcp.tool()
    async def notion_create_page(
        parent_id: str, title: str, content: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Create a page under a parent page.

        ``parent_id`` is a page id from ``notion_search`` (the integration must
        be shared on it). ``content`` becomes paragraphs, one per line. A body
        over Notion's one-request block ceiling is cut and flagged
        ``content_truncated`` — the rest is not written.

        **"unknown" means it may have been created: check before posting it
        again.** An error means it was not.
        """
        return await rpc.connector(
            "notion", "create_page", parent_id=parent_id, title=title, content=content,
            from_account=from_account,
        )

    @mcp.tool()
    async def notion_update_page(
        page_id: str, title: str = "", append_content: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Rename a page and/or append paragraphs to it.

        Pass ``title`` to rename, ``append_content`` to add paragraphs (one per
        line), or both. Appended text over Notion's one-request block ceiling is
        cut and flagged ``content_truncated`` — the rest is not written.

        **"unknown" means it may have been applied: check before trying again.**
        """
        return await rpc.connector(
            "notion", "update_page", page_id=page_id, title=title,
            append_content=append_content, from_account=from_account,
        )


def _register_calendar(mcp: FastMCP, rpc: Any) -> None:
    @mcp.tool()
    async def calendar_list_events(
        time_min: str = "", time_max: str = "", query: str = "", limit: int = 10,
        calendar_id: str = "primary", from_account: str = "",
    ) -> dict[str, Any]:
        """Events on a calendar, soonest first, each recurring event expanded to
        its individual occurrences.

        ``time_min``/``time_max`` are RFC-3339 timestamps bounding the window;
        ``query`` free-text matches an event. ``limit`` 1..2500 (default 10).
        ``calendar_id`` from ``calendar_list_calendars`` (``primary`` = the
        account's own).
        """
        return await rpc.connector(
            "googlecalendar", "list_events", time_min=time_min, time_max=time_max,
            query=query, limit=limit, calendar_id=calendar_id, from_account=from_account,
        )

    @mcp.tool()
    async def calendar_list_calendars(from_account: str = "") -> dict[str, Any]:
        """Calendars the connected account can see: id, name, whether it is the
        primary one, and the account's access role on it."""
        return await rpc.connector("googlecalendar", "list_calendars", from_account=from_account)

    @mcp.tool()
    async def calendar_create_event(
        summary: str, start: str, end: str, description: str = "", all_day: bool = False,
        calendar_id: str = "primary", from_account: str = "",
    ) -> dict[str, Any]:
        """Create an event. Nobody is invited and no notification is sent
        (attendees are out of scope for now).

        ``start``/``end`` are RFC-3339 timestamps for a timed event; for an
        all-day event pass ``all_day=true`` with ``start``/``end`` as
        ``YYYY-MM-DD`` dates (``end`` is exclusive).

        **"unknown" means it may have been created: check the calendar before
        creating it again.** An error means it was not.
        """
        # all_day is a bool here (FastMCP supports it), but the connector RPC body
        # rejects every bool value (rpc_service._check_op_body), so encode it as a
        # string the provider parses strictly.
        return await rpc.connector(
            "googlecalendar", "create_event", summary=summary, start=start, end=end,
            description=description, all_day="true" if all_day else "false",
            calendar_id=calendar_id, from_account=from_account,
        )

    @mcp.tool()
    async def calendar_update_event(
        event_id: str, summary: str = "", start: str = "", end: str = "",
        description: str = "", calendar_id: str = "primary", from_account: str = "",
    ) -> dict[str, Any]:
        """Change an event's summary, time, and/or description — pass only the
        fields to change. No notification is sent.

        ``start``/``end`` are RFC-3339 timestamps; ``event_id`` from
        ``calendar_list_events``.

        **"unknown" means it may have been applied: check before trying again.**
        """
        return await rpc.connector(
            "googlecalendar", "update_event", event_id=event_id, summary=summary,
            start=start, end=end, description=description, calendar_id=calendar_id,
            from_account=from_account,
        )

    @mcp.tool()
    async def calendar_delete_event(
        event_id: str, calendar_id: str = "primary", from_account: str = "",
    ) -> dict[str, Any]:
        """Delete an event. Deleting an already-gone event is a success. No
        notification is sent.

        **"unknown" means it may have been deleted: check before trying again.**
        """
        return await rpc.connector(
            "googlecalendar", "delete_event", event_id=event_id, calendar_id=calendar_id,
            from_account=from_account,
        )
