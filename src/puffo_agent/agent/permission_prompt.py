"""Shared constructor for operator-facing ``/permission`` prompts.

The ``/permission`` prefix renders an actionable Yes/No card in the
web/mobile clients; the buttons post ``y``/``n`` into the prompt's own
thread. Prompts must be sent as root-level DMs.
"""

from __future__ import annotations


def format_permission_prompt(
    intent: str,
    *,
    detail: str = "",
    reply_note: str = "",
) -> str:
    """Build a permission prompt with an optional detail quote."""
    line = (
        f"/permission {intent.strip()} "
        "Tap Yes/No, or reply `y`/`n` in this thread"
    )
    line += f" - {reply_note.strip()}." if reply_note.strip() else "."
    if detail.strip():
        quoted = "\n".join(
            f"> {detail_line}"
            for detail_line in detail.strip().splitlines()
        )
        line += f"\n\n{quoted}"
    return line
