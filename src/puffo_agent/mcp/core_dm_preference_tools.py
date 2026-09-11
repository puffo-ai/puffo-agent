"""MCP registration for dm preference tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import CONTEXT_VERSION
from ..crypto.http_client import HttpError
from .puffo_core_tools import _note_contact


def _require_signed(cfg: Any, tool: str, route: str) -> None:
    """Fail with the real reason when the route needs signed auth.

    ``/allowlists`` and ``/blocklists`` are subkey-signed on the server and
    have no keyless (``/v2/cloud-agents/*``) counterpart, so a keyless agent
    cannot use any of them. Every tool here says so the same way, instead of
    one saying it and the rest surfacing an opaque transport auth error that
    reads like something transient.
    """
    if cfg.keyless:
        raise RuntimeError(
            f"{tool} is unsupported for keyless agents: the {route} "
            "route requires signed auth this agent does not have"
        )


def register_dm_preference_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_dm_allowlists() -> dict[str, Any]:
        """List your DM allowlist (peers whose DMs skip the approval
        gate). Per-agent — every identity keeps its own list; this
        reads yours only."""
        _require_signed(cfg, "get_dm_allowlists", "/allowlists")
        data = await cfg.http_client.get("/allowlists")
        slugs = sorted(
            e.get("peer_slug", "")
            for e in (data.get("entries") or [])
            if e.get("peer_slug")
        )
        return {
            "context_version": CONTEXT_VERSION,
            "count": len(slugs),
            "identities": [f"@{slug.lstrip('@')}" for slug in slugs],
        }

    @mcp.tool()
    async def get_dm_blocklists() -> dict[str, Any]:
        """List your DM blocklist (senders whose messages are silently
        dropped). Per-agent — every identity keeps its own list; this
        reads yours only."""
        _require_signed(cfg, "get_dm_blocklists", "/blocklists")
        data = await cfg.http_client.get("/blocklists")
        slugs = sorted(
            b.get("id", "")
            for b in (data.get("blocks") or [])
            if b.get("target") == "user" and b.get("id")
        )
        return {
            "context_version": CONTEXT_VERSION,
            "count": len(slugs),
            "identities": [f"@{slug.lstrip('@')}" for slug in slugs],
        }

    @mcp.tool()
    async def add_dm_allowlist(slug: str) -> str:
        """Allow a user to DM you. Future DMs from this sender skip
        the ``auto_accept_dm`` approval prompt and deliver directly.
        Idempotent: re-allowlisting an entry is a no-op. Per-agent —
        only your own allowlist changes; other agents are unaffected.

        slug: peer to allow (e.g. ``alice-1234``).
        """
        target = (slug or "").strip()
        if not target:
            raise RuntimeError("slug is required")
        _require_signed(cfg, "add_dm_allowlist", "/allowlists")
        await cfg.http_client.post("/allowlists", {"slugs": [target]})
        _note_contact(cfg, target, allowed=True)
        return f"allowlisted {target}"

    @mcp.tool()
    async def update_dm_blocklist(slug: str, on: bool) -> str:
        """Block (``on=True``) or unblock (``on=False``) a sender.
        Blocked senders' messages never reach you — the server drops
        their DMs and this agent drops anything that still arrives.
        Per-agent — only your own blocklist changes; other agents are
        unaffected.

        slug: peer to (un)block (e.g. ``alice-1234``).
        on: ``True`` adds to blocklist, ``False`` removes.
        """
        target = (slug or "").strip()
        if not target:
            raise RuntimeError("slug is required")
        _require_signed(cfg, "update_dm_blocklist", "/blocklists")
        if on:
            await cfg.http_client.post(
                "/blocklists",
                {"target": "user", "id": target},
            )
            _note_contact(cfg, target, blocked=True)
            return f"blocked {target}"
        # DELETE /blocklists identifies the row by a JSON body
        # ``{"id": ...}``; 204 = removed, 404 = no such block. Both mean
        # the desired end state holds, so both settle local state — any
        # other failure propagates with local state untouched.
        try:
            await cfg.http_client.delete("/blocklists", body={"id": target})
        except HttpError as exc:
            if exc.status != 404:
                raise
            _note_contact(cfg, target, blocked=False)
            return f"unblocked {target} (was not blocked)"
        _note_contact(cfg, target, blocked=False)
        return f"unblocked {target}"
