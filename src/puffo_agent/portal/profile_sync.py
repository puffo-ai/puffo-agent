"""Shared helpers: PATCH ``/identities/self``, drop
refresh_agent.flag, push every server-tracked field in one shot
(startup full-sync), and extract the soul section from a
profile.md."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .state import AgentConfig

logger = logging.getLogger(__name__)


_DESCRIPTION_HEADINGS = {"soul", "description", "about", "summary"}


def _atx_heading(raw: str) -> tuple[int, str]:
    stripped = raw.lstrip()
    if not stripped.startswith("#"):
        return 0, ""
    i = 0
    while i < len(stripped) and stripped[i] == "#":
        i += 1
    if i < len(stripped) and stripped[i] in " \t":
        return i, stripped[i:].strip().lower()
    return 0, ""


def _soul_section_span(lines: list[str]) -> tuple[int, int, int] | None:
    """``(heading_idx, body_start, body_end)`` of the soul section, or
    None. A same-or-higher heading only closes the section once real
    prose has been collected — covers the legitimate case where the
    body opens with its own heading (e.g. ``# <agent-name>``)."""
    heading_idx = -1
    section_level = 0
    for idx, raw in enumerate(lines):
        level, text = _atx_heading(raw)
        if level and text in _DESCRIPTION_HEADINGS:
            heading_idx = idx
            section_level = level
            break
    if heading_idx == -1:
        return None
    body_start = heading_idx + 1
    body_end = len(lines)
    has_text = False
    for idx in range(body_start, len(lines)):
        raw = lines[idx]
        level, _ = _atx_heading(raw)
        if level:
            if level <= section_level and has_text:
                body_end = idx
                break
            continue
        if raw.strip():
            has_text = True
    return heading_idx, body_start, body_end


def extract_soul_body(profile_md_text: str) -> str:
    """Return the body of the ``# Soul`` section from a profile.md
    (or any description-like heading: description / about / summary).
    Trims surrounding blank lines. Empty when no such section
    exists. Single source of truth shared by the bridge read path
    and the server-sync paths."""
    lines = profile_md_text.splitlines()
    span = _soul_section_span(lines)
    if span is None:
        return ""
    _, body_start, body_end = span
    body_lines = lines[body_start:body_end]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    return "\n".join(body_lines)


async def sync_agent_profile(cfg: AgentConfig, patch: dict[str, Any]) -> None:
    """Push ``patch`` (any subset of display_name / avatar_url /
    role / role_short / soul) to the agent's server identity. Signed
    by the AGENT's subkey — callers own their own authorization
    gating before reaching here. Raises on HTTP / network failure.

    Keyless (T23 ``bridge`` transport) agents hold NO local signing
    identity and authenticate with an egress-injected
    ``x-sandbox-token``. The signed ``PATCH /identities/self`` here
    would drive ``_ensure_subkey`` → ``_rotate_subkey`` →
    ``KeyStore.load_identity`` and raise "identity not found: <slug>"
    on every warm/startup sync. The bridge exposes no profile-sync
    method, so there is nothing to route the patch over — skip the
    signed call entirely (native agents fall through unchanged). This
    is the single choke point every profile-sync caller reaches
    (sync_full_profile, cli, api handlers, control link), so the guard
    belongs here."""
    pc = cfg.puffo_core
    if pc.transport == "bridge":
        logger.debug(
            "sync_agent_profile: skipping signed PATCH for keyless "
            "bridge agent=%s (no local identity; no bridge sync route)",
            cfg.id,
        )
        return

    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(pc.server_url, ks, pc.slug)
    try:
        await http.patch("/identities/self", patch)
    finally:
        await http.close()


def write_refresh_agent_flag(cfg: AgentConfig, *, reason: str) -> None:
    """Drop ``refresh_agent.flag`` so the worker rebuilds its system
    prompt on the next batch. Best-effort."""
    from .state import refresh_agent_flag_path
    flag_path = refresh_agent_flag_path(cfg.resolve_workspace_dir())
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
            "refresh_agent.flag write failed for agent=%s (%s): %s",
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
