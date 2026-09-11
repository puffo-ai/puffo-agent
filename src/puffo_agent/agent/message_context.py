"""Pure helpers for constructing prompt-facing message context."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping


logger = logging.getLogger(__name__)

# Mirrors the web client's remark-mentions pattern; the non-word leader keeps
# addresses such as ``foo@bar-1234`` from becoming mentions.
MENTION_RE = re.compile(
    r"(?:^|\W)@([a-z][a-z0-9-]*-[a-f0-9]{4})", re.IGNORECASE,
)

_LONG_MESSAGE_PREVIEW_CHARS = 240


def content_with_visibility(
    content: object,
    *,
    is_visible_to_human: bool,
) -> dict[str, Any]:
    """Preserve message content while attaching its authenticated visibility."""
    if isinstance(content, Mapping):
        projected = dict(content)
    else:
        projected = {"text": "" if content is None else str(content)}
    projected["is_visible_to_human"] = bool(is_visible_to_human)
    return projected


def maybe_redact_long_text(
    text: str,
    *,
    envelope_id: str,
    sender_slug: str,
    sender_display_name: str,
    max_inline_chars: int,
    segment_chars: int,
    agent_slug: str,
) -> str:
    """Replace oversized prompt text with a pageable message reference."""
    if not text:
        return text
    total = len(text)
    if total <= max_inline_chars:
        return text

    segment_count = (total + segment_chars - 1) // segment_chars
    preview = text[:_LONG_MESSAGE_PREVIEW_CHARS].replace("\n", " ").strip()
    if total > _LONG_MESSAGE_PREVIEW_CHARS:
        preview += "…"

    sender_label = (
        f"@{sender_display_name} ({sender_slug})"
        if sender_display_name
        else f"@{sender_slug}"
    )
    placeholder = (
        "[puffo-agent system message] inbound message was too long "
        "to embed inline and has been redacted from this prompt for "
        "context-budget reasons.\n"
        f"  envelope_id: {envelope_id}\n"
        f"  total_chars: {total}\n"
        f"  segments: {segment_count} (0-indexed, up to {segment_chars} chars each)\n"
        f"  sender: {sender_label}\n"
        f"  preview: {preview}\n"
        "Retrieve the full body one chunk at a time with "
        "mcp__puffo__get_post_segment("
        f'envelope_id="{envelope_id}", segment=N, '
        f"segment_size={segment_chars}) where N runs "
        f"0..{segment_count - 1}. Fetch only the segments you actually "
        "need — the placeholder above already tells you what kind "
        "of content it is."
    )
    logger.info(
        "agent %s: inlined message %s truncated (%d → %d chars, %d segments) "
        "for prompt budget",
        agent_slug,
        envelope_id,
        total,
        max_inline_chars,
        segment_count,
    )
    return placeholder
