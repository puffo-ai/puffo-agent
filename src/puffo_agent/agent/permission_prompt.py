"""Shared constructor for operator-facing ``/permission`` prompts.

The ``/permission`` prefix renders an actionable Yes/No card in the
web/mobile clients; the buttons post ``y``/``n`` into the prompt's own
thread. Must be sent as a root-level DM (the card only renders there).
"""

from __future__ import annotations


def format_permission_prompt(
    intent: str,
    *,
    detail: str = "",
    reply_note: str = "",
) -> str:
    """``/permission <intent>`` + the standard Yes/No instruction;
    ``detail`` renders as a quote block, ``reply_note`` extends the
    instruction line."""
    line = f"/permission {intent.strip()} Tap Yes/No, or reply `y`/`n` in this thread"
    line += f" — {reply_note.strip()}." if reply_note.strip() else "."
    if detail.strip():
        quoted = "\n".join(f"> {ln}" for ln in detail.strip().splitlines())
        line += f"\n\n{quoted}"
    return line
