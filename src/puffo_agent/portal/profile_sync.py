"""Shared helper: PATCH ``/identities/self`` signed by an agent's
own keystore. Used by both the local bridge edit handler and the
``puffo-agent agent profile`` CLI so they speak the same wire format
without duplicating crypto plumbing.
"""

from __future__ import annotations

from typing import Any

from .state import AgentConfig


async def sync_agent_profile(cfg: AgentConfig, patch: dict[str, Any]) -> None:
    """Push ``patch`` (display_name / avatar_url / role / role_short)
    to the agent's server-side identity profile. The PATCH is signed
    by the AGENT's subkey (not the operator's) — both the bridge
    handler and the CLI call this with the same shape; the bridge
    enforces operator-only authorization before reaching this point,
    and the CLI relies on the operator already controlling the local
    keystore.

    Raises any HTTP / network failure to the caller so they can decide
    whether to fail loud (CLI) or warn and continue (bridge)."""
    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    pc = cfg.puffo_core
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        await http.patch("/identities/self", patch)
    finally:
        await http.close()
