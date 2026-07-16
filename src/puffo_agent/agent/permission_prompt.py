"""Shared constructor for operator-facing ``/permission`` prompts.

Every daemon flow that asks the operator for a y/n decision (space and
channel invites, agent-requested leaves, cli-local command permission,
foreign-DM approval) builds its DM through ``format_permission_prompt``
so the web/mobile clients render one consistent actionable card: the
``/permission`` prefix triggers Yes/No buttons, which post ``y``/``n``
into the prompt's own thread — the same replies the daemon-side
intercepts already accept.

The prompt must be sent as a root-level DM to the operator (the card
only renders there); the returned envelope_id keys the pending entry
the y/n reply routes back to.
"""

from __future__ import annotations


def format_permission_prompt(
    intent: str,
    *,
    detail: str = "",
    reply_note: str = "",
) -> str:
    """``/permission <intent>`` + the standard Yes/No instruction.

    intent: one sentence asking the question (may carry markdown).
    detail: optional context rendered as a quote block (a message
        preview, a command summary, a reason).
    reply_note: optional extra reply semantics appended to the
        instruction line (e.g. bulk-reply behavior).
    """
    line = f"/permission {intent.strip()} Tap Yes/No, or reply `y`/`n` in this thread"
    line += f" — {reply_note.strip()}." if reply_note.strip() else "."
    if detail.strip():
        quoted = "\n".join(f"> {ln}" for ln in detail.strip().splitlines())
        line += f"\n\n{quoted}"
    return line
