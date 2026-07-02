"""Shared visibility resolution for outbound messages.

Both the MCP send_message tools (agent-driven) and the fallback path
in ``puffo_core_client.send_fallback_message`` (worker posts when the
LLM produced text but skipped the tool call) route through
``resolve_visibility`` so all outbound sends honour the same floor
and the same per-level guidance notes.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .puffo_core_client import _MENTION_RE


logger = logging.getLogger(__name__)


_VISIBILITY_LEVELS = ("human", "default", "agent_only")


async def resolve_visibility(
    level: str,
    channel_ref: str,
    text: str,
    root_id: str,
    http_client: Any,
) -> tuple[bool, str]:
    """Return ``(effective_is_visible_to_human, note)`` for an
    outbound message.

    ``level`` is ``"human"``, ``"default"``, or ``"agent_only"``.
    Semantics:

    - ``human`` — send visible, no note.
    - ``default`` — send hidden BUT force visible on DMs, root-level
      posts, or @-mentions of a human. When forced visible the note
      explains why + nudges the caller toward the explicit level next
      time; when NOT forced, the note nudges the caller to be
      explicit anyway.
    - ``agent_only`` — send hidden regardless of DM/@-mention (agent
      opted out), except root-level which always coerces (can't
      fold). Note warns when the message LOOKS like a human should
      see it so the caller can reconsider.

    Profile-lookup failures on the @-mention check pass through
    silently — a transient error can't flip an intentional hidden
    send.
    """
    if level not in _VISIBILITY_LEVELS:
        raise RuntimeError(
            f"visibility_level must be one of {_VISIBILITY_LEVELS!r}; "
            f"got {level!r}"
        )
    if level == "human":
        return True, ""

    # Root-level auto-coerce (can't fold either way; applies to both
    # default and agent_only).
    if not root_id.strip():
        return True, (
            "\nnote: hidden ignored — root-level messages can't fold, "
            "so this is sent visible regardless. Only threaded replies "
            "(with root_id set) can actually be hidden."
        )

    signal, reason = await _detect_human_signal(channel_ref, text, http_client)

    if level == "default":
        if signal:
            return True, _default_coerced_note(reason)
        return False, _default_nudge_note()
    # level == "agent_only"
    if signal:
        return False, _agent_only_warn_note(reason)
    return False, ""


async def _detect_human_signal(
    channel_ref: str, text: str, http_client: Any,
) -> tuple[bool, str]:
    """``(True, "dm")`` if DM; ``(True, "mention")`` if body
    @-mentions at least one human; ``(False, "")`` otherwise. Profile
    lookup errors are swallowed so a transient failure reads as "no
    signal" (preserves caller intent)."""
    if channel_ref.startswith("@"):
        return True, "dm"
    mentioned = sorted({m.lower() for m in _MENTION_RE.findall(text or "")})
    if not mentioned:
        return False, ""
    try:
        resp = await http_client.get(
            "/identities/profiles?slugs="
            + ",".join(urllib.parse.quote(m, safe="") for m in mentioned)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "visibility-floor profile fetch failed for %s: %s",
            mentioned, exc,
        )
        return False, ""
    profiles = resp.get("profiles", []) if isinstance(resp, dict) else []
    for p in profiles:
        if (p.get("identity_type") or "human").strip().lower() == "human":
            return True, "mention"
    return False, ""


def _default_coerced_note(reason: str) -> str:
    ctx = (
        "this is a DM, so the addressee is waiting for a reply"
        if reason == "dm"
        else "the message @-mentions a human"
    )
    return (
        f"\nnote: sent visible — {ctx}. You used "
        "``visibility_level='default'``; for future messages that a "
        "person should read, pass ``'human'`` explicitly so this "
        "safety-net doesn't have to guess."
    )


def _default_nudge_note() -> str:
    return (
        "\nnote: sent hidden with ``visibility_level='default'``. Try "
        "to be explicit next turn: ``'human'`` when a person should "
        "read the message, ``'agent_only'`` when it's genuinely "
        "agent-to-agent."
    )


def _agent_only_warn_note(reason: str) -> str:
    ctx = (
        "this is a DM, so the addressee usually is waiting for a reply"
        if reason == "dm"
        else "the message @-mentions a human"
    )
    return (
        f"\nnote: sent hidden per ``visibility_level='agent_only'``, "
        f"but {ctx}. Double-check this really is agent-to-agent — "
        "``'human'`` would have surfaced it to the person."
    )
