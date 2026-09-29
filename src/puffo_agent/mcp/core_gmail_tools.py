from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


def register_gmail_tools(mcp: FastMCP, cfg: Any) -> None:
    # Sending goes through the daemon, which holds the agent's credentials;
    # without the RPC link (ws-local) there is nothing to send with.
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
