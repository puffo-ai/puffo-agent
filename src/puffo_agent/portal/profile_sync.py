"""Shared helper: PATCH ``/identities/self`` signed by an agent's
own keystore. Used by both the local bridge edit handler and the
``puffo-agent agent profile`` CLI so they speak the same wire format
without duplicating crypto plumbing.
"""

from __future__ import annotations

from typing import Any

from .state import AgentConfig


async def sync_agent_profile(cfg: AgentConfig, patch: dict[str, Any]) -> None:
    """Push ``patch`` (any subset of display_name / avatar_url /
    role / role_short / soul) to the agent's server identity. Signed
    by the AGENT's subkey — callers (bridge / CLI / link-migrate)
    own their own authorization gating before reaching here. Raises
    on HTTP / network failure."""
    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    pc = cfg.puffo_core
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        await http.patch("/identities/self", patch)
    finally:
        await http.close()
