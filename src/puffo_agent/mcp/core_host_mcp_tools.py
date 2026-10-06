"""MCP registration for host mcp tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP


def register_host_mcp_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register every host-side tool. One helper per tool group, so each stays
    readable on its own and the structure hook's function-length limit holds."""
    _register_install(mcp, cfg)
    _register_credential_request(mcp, cfg)
    _register_sync(mcp, cfg)


def _register_install(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def install_host_mcp(
        name: str,
        spec: Optional[dict] = None,
        template_id: str = "",
    ) -> str:
        """Lay down an MCP server spec into the operator's host
        ``~/.claude.json`` so they can complete OAuth / paste API keys
        on their own claude session, then auto-DM them a one-line
        install confirmation. Pair with ``sync_host_mcp`` once they
        confirm. If you have setup-context to share (docs URL, env
        keys to populate, gotchas) send a separate follow-up message
        — the auto-DM is intentionally minimal.

        ``name``: the key the entry registers under
            (``mcpServers[<name>]`` on host).

        Pass exactly ONE of the two source forms:

        - ``template_id``: look up the spec from puffo-server's
          ``/v2/mcp-templates/<id>`` catalog. Use when the MCP is
          operator-curated and ``desired_mcp`` ships an empty-env
          placeholder you need credentials for.
        - ``spec``: pass an inline MCP server config dict transcribed
          from the MCP package's own README — useful when you find
          an MCP on the web (e.g. Coinbase CDP MCP) that isn't in
          puffo-server's catalog. Shape:
            ``{"type": "stdio", "command": "npx", "args": [...], "env": {...}}``
            ``{"type": "http"|"sse", "url": "https://...", "env": {...}}``
          Set ``env`` values to empty strings for placeholders the
          operator needs to populate.

        Behaviour:
          - host already has the entry → file untouched, no DM, tells
            you to skip to ``sync_host_mcp``.
          - catalog / spec validation / file write fails → tool errors,
            no side effects.
          - host write succeeds + DM succeeds → returns the DM's
            envelope_id; wait for the operator's ping.
          - host write succeeds + DM fails → returns the prebuilt body
            so you can retry via ``send_message`` yourself.
        """
        if cfg.rpc_client is None:
            raise RuntimeError(
                "install_host_mcp unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        return await cfg.rpc_client.install_mcp(
            name=name,
            template_id=template_id,
            spec=spec,
        )


def _register_credential_request(mcp: FastMCP, cfg: Any) -> None:
    """``request_credential`` + ``credential_status`` (``credential_requests``)."""
    @mcp.tool()
    async def request_credential(type: str, reason: str, alias: str = "") -> str:
        """Ask your owner for a credential you need, through a secure form in
        your DM. The value is encrypted to you and filed through the
        credentials API — it is NEVER sent as a message and you never see it.

        ``type``: one of
          - ``PUFFO_CHATGPT_CREDENTIAL_JSON_v1`` — a ChatGPT plan (Codex) credential
          - ``PUFFO_CLAUDE_CODE_TOKEN_v1`` — a Claude Code plan token (Linux/cloud only)
          - ``CUSTOMIZED`` — any other secret; give it an ``alias`` (an env
            var name, e.g. ``GITHUB_TOKEN``) and you will find it in your
            environment under that name after placement.
        ``reason``: one plain line the owner reads on the form.

        What happens next: the owner's reply arrives in the DM as a
        ``puffo-credential-filed`` block. The daemon places the credential
        BEFORE you see that message and restarts your CLI to pick it up —
        your session and transcript survive. Then call ``credential_status``
        with the request_id and, only when it says ``placed``, tell the owner
        in plain words that it is done. Never ask the owner to paste a secret
        into chat.
        """
        if cfg.rpc_client is None:
            raise RuntimeError(
                "request_credential unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        return await cfg.rpc_client.request_credential(
            type=type, reason=reason, alias=alias,
        )

    @mcp.tool()
    async def credential_status(request_id: str) -> str:
        """Where a ``request_credential`` stands: pending, ``placed <type>
        #<index> v<version>``, or ``failed: <reason>``. Never contains the value.

        **``pending`` does NOT mean the owner failed to file it.** Filing is
        asynchronous: the owner submits through the secure form, the value is
        encrypted and filed through the credentials API, and only then does it
        reach you. Until it does, the one thing you may tell the owner is that
        it has not arrived on your side yet. Never say the filing failed, and
        never ask them to fill the form or paste the value again — a second
        submission creates a duplicate credential and may cost them a rotation.
        Check again instead, and say ``placed`` only when this tool says so."""
        if cfg.rpc_client is None:
            raise RuntimeError(
                "credential_status unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        return await cfg.rpc_client.credential_status(request_id=request_id)


def _register_sync(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def sync_host_mcp(template_id: str) -> str:
        """Copy the operator's ``~/.claude.json#mcpServers[<id>]``
        entry into your own ``<agent>/.claude.json``. Pair with
        ``install_host_mcp`` once the operator finishes OAuth on host,
        then the runtime automatically reloads the provider at the next
        idle boundary so it picks up the new MCP.

        If the host config doesn't have the entry yet, returns an
        error asking you to call ``install_host_mcp`` first (and
        relay the result to the operator).
        """
        if cfg.rpc_client is None:
            raise RuntimeError(
                "sync_host_mcp unavailable — PUFFO_RPC_URL not set "
                "on this MCP runtime, so the puffo-agent daemon's "
                "rpc_service isn't reachable."
            )
        result = await cfg.rpc_client.sync_mcp(template_id=template_id)
        workspace = getattr(cfg, "workspace", None)
        synced = result.startswith(("Verified host's ", "Synced host's "))
        if workspace and synced:
            from .host_tools import _touch_refresh_flag

            _touch_refresh_flag(Path(workspace), "refresh_agent")
            result += " Runtime refresh requested."
        return result
