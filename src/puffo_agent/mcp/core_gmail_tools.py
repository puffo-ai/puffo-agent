from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_gmail_tools(mcp: FastMCP, cfg: Any) -> None:
    # The daemon holds the credentials; no RPC link, nothing to send with.
    if getattr(cfg, "rpc_client", None) is None:
        return
    _register_sending(mcp, cfg)
    _register_reading(mcp, cfg)
    _register_filing(mcp, cfg)


def _register_sending(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def gmail_send(
        to: str, subject: str, body: str, cc: str = "", bcc: str = "",
        html_body: str = "", reply_to_message_id: str = "",
        forward_message_id: str = "", attachments: list[str] | None = None,
        from_account: str = "",
    ) -> dict[str, Any]:
        """Send an email from the Google account your operator shared with you.

        ``to``, ``cc`` and ``bcc`` take one address or a comma-separated
        list. ``body`` is plain text; ``html_body`` adds an HTML version.

        To answer or pass on a message, give its id as
        ``reply_to_message_id`` or ``forward_message_id``: the email stays in
        that conversation. A reply with empty ``to`` goes to the sender, and
        an empty ``subject`` becomes "Re: …" or "Fwd: …". A forward carries
        the original, attachments included.

        ``attachments`` are paths relative to your workspace (at most 10,
        25 MiB in all). ``from_account`` is only needed when several
        accounts are shared; the error lists the names to choose from.

        **"unknown" means it may have been sent: do not send it again** —
        tell the user, or check the Sent folder. An error means it was not.
        Prefer ``gmail_create_draft`` when the user should see it first.
        """
        return await cfg.rpc_client.gmail_send(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, html_body=html_body,
            reply_to_message_id=reply_to_message_id,
            forward_message_id=forward_message_id, attachments=attachments or [],
            from_account=from_account,
        )

    @mcp.tool()
    async def gmail_create_draft(
        to: str = "", subject: str = "", body: str = "", cc: str = "", bcc: str = "",
        html_body: str = "", reply_to_message_id: str = "",
        forward_message_id: str = "", attachments: list[str] | None = None,
        from_account: str = "",
    ) -> dict[str, Any]:
        """Save an email as a draft for the user to review, instead of sending.

        Takes everything ``gmail_send`` takes. Returns a ``draft_id`` for
        ``gmail_send_draft``. "unknown" means a draft may exist: check
        ``gmail_list_drafts`` before creating it again.
        """
        return await cfg.rpc_client.gmail_draft(
            "create", to=to, subject=subject, body=body, cc=cc, bcc=bcc,
            html_body=html_body, reply_to_message_id=reply_to_message_id,
            forward_message_id=forward_message_id, attachments=attachments or [],
            from_account=from_account,
        )

    @mcp.tool()
    async def gmail_send_draft(draft_id: str, from_account: str = "") -> dict[str, Any]:
        """Send a draft by its ``draft_id``.

        **"unknown" means it may have been sent: do not send it again**
        without checking the Sent folder.
        """
        return await cfg.rpc_client.gmail_draft(
            "send", draft_id=draft_id, from_account=from_account,
        )


def _register_reading(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def gmail_search(
        query: str = "", limit: int = 10, page_token: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Find mail in the mailbox your operator shared with you.

        ``query`` is Gmail search syntax — ``is:unread``,
        ``from:alice@example.com``, ``newer_than:7d`` — and combinations;
        empty means the most recent mail. ``limit`` is 1..50. When
        ``next_page_token`` is set there are more: pass it back as
        ``page_token`` with the same query. Each result carries a
        ``message_id`` for the other tools.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "search", query=query, limit=limit, page_token=page_token,
            from_account=from_account,
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
    async def gmail_get_attachment(
        message_id: str, filename: str, from_account: str = "",
    ) -> dict[str, Any]:
        """Save one attachment into your workspace, by the ``filename``
        ``gmail_read`` listed. Returns its ``path`` relative to the workspace.
        Saving again overwrites, so retrying is safe.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "get_attachment", message_id=message_id, filename=filename,
            from_account=from_account,
        )

    @mcp.tool()
    async def gmail_list_drafts(
        limit: int = 10, page_token: str = "", from_account: str = "",
    ) -> dict[str, Any]:
        """Drafts, newest first, each with the ``draft_id`` that sends it.
        Pages like ``gmail_search``."""
        return await cfg.rpc_client.gmail_mailbox(
            "list_drafts", limit=limit, page_token=page_token, from_account=from_account,
        )


def _register_filing(mcp: FastMCP, cfg: Any) -> None:
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

    @mcp.tool()
    async def gmail_list_labels(from_account: str = "") -> dict[str, Any]:
        """Every label, system ones (INBOX, UNREAD, …) included, with ids."""
        return await cfg.rpc_client.gmail_mailbox("list_labels", from_account=from_account)

    @mcp.tool()
    async def gmail_create_label(name: str, from_account: str = "") -> dict[str, Any]:
        """Create a label. One that already exists is returned, not
        duplicated, so retrying is safe. ``A/B`` nests B under A."""
        return await cfg.rpc_client.gmail_mailbox(
            "create_label", name=name, from_account=from_account,
        )

    @mcp.tool()
    async def gmail_label(
        message_ids: list[str], add: list[str] | None = None,
        remove: list[str] | None = None, from_account: str = "",
    ) -> dict[str, Any]:
        """Add and remove labels on up to 1000 messages at once.

        Labels are given by name or id. A name that does not exist is an
        error rather than a new label: create it first. Idempotent, so
        retrying is safe.
        """
        return await cfg.rpc_client.gmail_mailbox(
            "label", message_ids=message_ids, add=add or [], remove=remove or [],
            from_account=from_account,
        )
