from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_gmail_tools(mcp: FastMCP, cfg: Any) -> None:
    # The daemon holds the credentials; no RPC link, nothing to send with.
    if getattr(cfg, "rpc_client", None) is None:
        return

    @mcp.tool()
    async def gmail_send(
        to: str, subject: str, body: str, from_account: str = "",
    ) -> dict[str, Any]:
        """Send an email from the Google account your operator shared with you.

        ``to`` is one address or a comma-separated list; ``body`` is plain
        text. ``from_account`` is only needed when several accounts are
        shared; the error lists the names to choose from.

        **"unknown" means it may have been sent: do not send it again** —
        tell the user, or check the Sent folder. An error means it was not.
        """
        return await cfg.rpc_client.gmail_send(
            to=to, subject=subject, body=body, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_search(
        query: str = "", limit: int = 10, from_account: str = "",
    ) -> dict[str, Any]:
        """Find mail in the mailbox your operator shared with you.

        ``query`` is Gmail search syntax — ``is:unread``,
        ``from:alice@example.com``, ``newer_than:7d`` — and combinations;
        empty means the most recent mail. ``limit`` is 1..50. Each result
        carries a ``message_id`` for the other two tools.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "search", query=query, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_read(message_id: str, from_account: str = "") -> dict[str, Any]:
        """Read one message in full, by the ``message_id`` a search returned.

        Headers, the body as plain text, and attachment names. A long body
        is cut and ``body_truncated`` says so. Does not mark it read.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "read", message_id=message_id, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_organize(
        message_id: str, action: str, from_account: str = "",
    ) -> dict[str, Any]:
        """File a message: ``archive``, ``move_to_inbox``, ``mark_read``,
        ``mark_unread``, ``star``, ``unstar``, ``trash`` or ``untrash``.

        ``trash`` is reversible with ``untrash``; nothing deletes mail
        permanently. Every action is idempotent, so retrying is safe.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "organize", message_id=message_id, action=action, from_account=from_account,
        )
