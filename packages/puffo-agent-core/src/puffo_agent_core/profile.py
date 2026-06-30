"""Profile (``# Soul``) section parsing.

Pure string code. The canonical ``extract_soul_body`` shared by the
local agent's server-sync paths and the cloud runtime's system-prompt
read path; ``puffo_agent.portal.profile_sync`` re-exports it."""

from __future__ import annotations


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
