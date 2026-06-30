"""Shared helpers: PATCH ``/identities/self``, write reload.flag,
push every server-tracked field in one shot (startup full-sync),
and extract the soul section from a profile.md."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .state import AgentConfig

# The soul-section parser is pure string code; it now lives in the
# stdlib-only kernel so the slim cloud runtime can reuse it. Re-export
# both the public reader and the ``_soul_section_span`` helper (the
# write path in ``portal/api/handlers.py`` imports the latter from here
# as a shim) so existing call sites are unaffected.
from puffo_agent_core.profile import (  # noqa: F401
    _soul_section_span,
    extract_soul_body,
)

logger = logging.getLogger(__name__)


async def sync_agent_profile(cfg: AgentConfig, patch: dict[str, Any]) -> None:
    """Push ``patch`` (any subset of display_name / avatar_url /
    role / role_short / soul) to the agent's server identity. Signed
    by the AGENT's subkey — callers own their own authorization
    gating before reaching here. Raises on HTTP / network failure."""
    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    pc = cfg.puffo_core
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        await http.patch("/identities/self", patch)
    finally:
        await http.close()


def write_reload_flag(cfg: AgentConfig, *, reason: str) -> None:
    """Drop ``reload.flag`` so the worker rebuilds its system prompt
    on the next batch. Best-effort."""
    flag_path = cfg.resolve_workspace_dir() / ".puffo-agent" / "reload.flag"
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(
            json.dumps({
                "version": 1,
                "requested_at": int(time.time()),
                "reason": reason,
            }) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "reload.flag write failed for agent=%s (%s): %s",
            cfg.id, reason, exc,
        )


async def sync_full_profile(cfg: AgentConfig) -> None:
    """One-shot PATCH of every server-tracked profile field plus the
    ``# Soul`` section extracted from profile.md. Soul is omitted
    when profile.md is missing OR has no soul-like heading — the
    server's stored value is preserved rather than clobbered."""
    patch: dict[str, Any] = {
        "display_name": cfg.display_name,
        "role": cfg.role,
        "role_short": cfg.role_short,
        "avatar_url": cfg.avatar_url,
    }
    try:
        text = cfg.resolve_profile_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError as exc:
        logger.warning(
            "sync_full_profile: profile.md read failed for agent=%s: %s",
            cfg.id, exc,
        )
        text = None
    if text is not None:
        soul = extract_soul_body(text)
        if soul:
            patch["soul"] = soul
    await sync_agent_profile(cfg, patch)
