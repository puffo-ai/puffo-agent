"""Profile validation and markdown mutation helpers."""

from __future__ import annotations

import logging

from ..profile_sync import _soul_section_span, extract_soul_body
from ..state import AgentConfig

logger = logging.getLogger(__name__)

MAX_ROLE_LEN = 140
MAX_ROLE_SHORT_LEN = 32
MAX_PROFILE_SUMMARY_BYTES = 10000


def profile_summary(cfg: AgentConfig) -> str:
    """Return the profile's soul-section body, or an empty string."""
    try:
        text = cfg.resolve_profile_path().read_text(encoding="utf-8")
    except Exception:
        return ""
    return extract_soul_body(text)


def derive_role_short(role: str) -> str:
    """Mirror puffo-server's short role-label derivation."""
    if ":" not in role:
        return ""
    colon_pos = role.index(":")
    candidate = role[:colon_pos].strip()
    rest = role[colon_pos + 1 :].strip()
    if not candidate or not rest:
        return ""
    if len(candidate) > MAX_ROLE_SHORT_LEN:
        return ""
    if any(ch.isspace() for ch in candidate):
        return ""
    return candidate


def update_profile_summary(cfg: AgentConfig, new_summary: str) -> None:
    """Replace the profile's soul section, appending one if absent."""
    try:
        path = cfg.resolve_profile_path()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        span = _soul_section_span([raw.rstrip("\r\n") for raw in lines])
        if span is not None:
            _, body_start, body_end = span
            new_lines = (
                lines[:body_start] + ["\n", new_summary + "\n"] + lines[body_end:]
            )
        else:
            new_lines = list(lines)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.extend(["\n# Soul\n", new_summary + "\n"])
        path.write_text("".join(new_lines), encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "bridge: failed to update profile summary for agent=%s: %s",
            cfg,
            exc,
        )


def update_profile_role(cfg: AgentConfig, new_role: str) -> None:
    """Rewrite the first ``**Role:**`` line in the profile."""
    if not new_role.strip():
        return
    try:
        path = cfg.resolve_profile_path()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, raw in enumerate(lines):
            if raw.rstrip("\r\n").startswith("**Role:**"):
                ending = raw[len(raw.rstrip("\r\n")) :] or "\n"
                lines[index] = f"**Role:** {new_role.strip()}{ending}"
                path.write_text("".join(lines), encoding="utf-8")
                return
    except Exception as exc:
        logger.warning(
            "bridge: failed to update profile role for agent=%s: %s",
            cfg,
            exc,
        )


# Compatibility names retained by ``portal.api.handlers``.
_derive_role_short = derive_role_short
_profile_summary = profile_summary
_update_profile_role = update_profile_role
_update_profile_summary = update_profile_summary
