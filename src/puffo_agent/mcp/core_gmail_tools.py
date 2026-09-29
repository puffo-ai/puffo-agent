from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_gmail_tools(mcp: FastMCP, cfg: Any) -> None:
    # The daemon holds the credentials; without the RPC link there is
    # nothing to send with.
    if getattr(cfg, "rpc_client", None) is None:
        return

    @mcp.tool()
    async def gmail_send(
        to: str, subject: str, body: str, from_account: str = "",
    ) -> dict[str, Any]:
        """Send an email from the Google account your operator shared with you.

        ``to`` is one address or a comma-separated list; ``body`` is plain
        text. ``from_account`` picks the account when more than one is shared:
        pass one of the names the error lists (normally the account's email,
        but it is the name the owner gave it). Leave it empty otherwise.

        Returns ``{"status": "sent", "message_id": ...}``, or
        ``{"status": "unknown", ...}`` when the request went out but the
        answer was lost. **"unknown" means it may have been sent: do not
        send it again**; tell the user, or check the Sent folder first. An
        error means it was definitely not sent.
        """
        return await cfg.rpc_client.gmail_send(
            to=to, subject=subject, body=body, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_search(
        query: str = "", limit: int = 10, from_account: str = "",
    ) -> dict[str, Any]:
        """Find mail in the mailbox your operator shared with you.

        ``query`` is Gmail search syntax, the same as the Gmail search box:
        ``is:unread``, ``from:alice@example.com``, ``subject:invoice``,
        ``newer_than:7d``, ``has:attachment``, and combinations. Empty means
        the most recent mail. ``limit`` is 1..50.

        Returns ``{"messages": [...], "count": n}``; each message carries a
        ``message_id`` for ``gmail_read`` and ``gmail_organize``, plus from,
        subject, date, a snippet and whether it is unread. Reading never
        changes anything, so a failure here is always safe to retry.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "search", query=query, limit=limit, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_read(message_id: str, from_account: str = "") -> dict[str, Any]:
        """Read one message in full, by the ``message_id`` a search returned.

        Returns the headers, the plain-text body (HTML mail is converted to
        text) and the names of any attachments. A very long body is cut and
        ``body_truncated`` says so. Reading does not mark the message read;
        use ``gmail_organize`` for that.
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

        ``trash`` is reversible with ``untrash``; nothing here deletes mail
        permanently, because that access was never granted. Every action is
        idempotent, so repeating one is harmless and a failure is safe to
        retry.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "organize", message_id=message_id, action=action, from_account=from_account,
        )
